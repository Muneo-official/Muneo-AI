"""cross-encoder 점수에서 텍스트 길이의 선형 효과를 통계적으로 제거(de-bias)한 뒤 재정렬 —
캡으로 정보를 버리는 대신, 정보는 그대로 두고 길이가 점수에 준 영향만 빼는 방식을 시도한다.

배경: eval/results/reranker_hybrid_eval.md "확정" 절에서 cross-encoder가 텍스트 길이 자체에
편향된다는 게 인과관계로 확인됐다. eval/case_text_cap_experiment.py로 트렁케이션 길이를 줄이는
완화책을 시도했지만 precision@5/precision@10 사이에 트레이드오프가 남았다(정보를 버리기 때문).
이 스크립트는 텍스트를 자르지 않고 원문(cap=300, 현재 프로덕션과 동일) 그대로 cross-encoder에
넣되, 같은 쿼리의 후보 풀(20건) 안에서 "점수 vs 텍스트 길이"의 1차 선형관계를 구해서 그 관계로
설명되는 부분을 빼고 남은 잔차(residual)로 재정렬한다 — 길이가 유난히 길어서/짧아서 받은 보너스·
페널티만 통계적으로 상쇄하고, 실제 내용에서 온 점수 차이는 그대로 남기려는 시도.
"""

import asyncio
import json
import pathlib
import statistics

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine

load_dotenv()

BASE = pathlib.Path(__file__).parent / "test_inputs"
K_VALUES = [5, 10]
CANDIDATE_POOL = 20
TOP_K = 15


def _linear_residuals(xs: list[float], ys: list[float]) -> list[float]:
    """단순 1차 회귀(최소제곱) 잔차: y - (slope*x + intercept). 후보 수가 너무 적거나
    길이가 전부 같으면(분산 0) 회귀가 무의미하므로 원본 점수를 그대로 반환한다."""
    n = len(xs)
    if n < 3:
        return list(ys)
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0:
        return list(ys)
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    return [y - (slope * x + intercept) for x, y in zip(xs, ys)]


def _precision_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    top_k = ranked_ids[:k]
    if not top_k:
        return 0.0
    return sum(labels.get(a, 0) for a in top_k) / len(top_k)


def _pool_recall_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    total = sum(labels.values())
    if total == 0:
        return 0.0
    return sum(labels.get(a, 0) for a in ranked_ids[:k]) / total


async def main() -> None:
    queries = {q["query_id"]: q for q in json.loads((BASE / "queries.json").read_text(encoding="utf-8"))}
    pool = json.loads((BASE / "pool.json").read_text(encoding="utf-8"))
    by_query: dict[str, list[dict]] = {}
    for row in pool:
        by_query.setdefault(row["query_id"], []).append(row)

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    engine = EstimateEngine(case_repository=None, embedder=None, reranker=reranker)

    raw_scores = {f"precision@{k}": [] for k in K_VALUES}
    raw_scores |= {f"recall@{k}": [] for k in K_VALUES}
    debiased_scores = {f"precision@{k}": [] for k in K_VALUES}
    debiased_scores |= {f"recall@{k}": [] for k in K_VALUES}

    for query_id, rows in by_query.items():
        labels = {r["article_id"]: r["label"] for r in rows}
        vector_ranked = sorted((r for r in rows if r.get("vector_rank")), key=lambda r: r["vector_rank"])
        vector_ids = [r["article_id"] for r in vector_ranked][:CANDIDATE_POOL]
        if not vector_ids:
            continue

        docs = {
            d["article_id"]: d
            async for d in cases_col.find({"article_id": {"$in": vector_ids}}, {"embedding": 0})
        }
        ordered_cases = [docs[a] for a in vector_ids if a in docs]
        ids = [str(c.get("article_id")) for c in ordered_cases]
        texts = [EstimateEngine._case_text(c) for c in ordered_cases]

        query_text = engine.build_query(queries[query_id]["input"])
        pairs = [(query_text, t) for t in texts]
        ce_scores = list(await run_in_threadpool(reranker.predict, pairs))
        lengths = [float(len(t)) for t in texts]

        raw_order = sorted(range(len(ids)), key=lambda i: ce_scores[i], reverse=True)
        raw_ranked = [ids[i] for i in raw_order][:TOP_K]

        residuals = _linear_residuals(lengths, [float(s) for s in ce_scores])
        debiased_order = sorted(range(len(ids)), key=lambda i: residuals[i], reverse=True)
        debiased_ranked = [ids[i] for i in debiased_order][:TOP_K]

        for k in K_VALUES:
            raw_scores[f"precision@{k}"].append(_precision_at_k(raw_ranked, labels, k))
            raw_scores[f"recall@{k}"].append(_pool_recall_at_k(raw_ranked, labels, k))
            debiased_scores[f"precision@{k}"].append(_precision_at_k(debiased_ranked, labels, k))
            debiased_scores[f"recall@{k}"].append(_pool_recall_at_k(debiased_ranked, labels, k))

    client.close()

    print(f"{'지표':<14}{'CE 원점수':>12}{'길이 de-bias':>14}{'변화':>10}")
    for k in K_VALUES:
        for metric in ("precision", "recall"):
            key = f"{metric}@{k}"
            raw = statistics.mean(raw_scores[key]) if raw_scores[key] else 0.0
            deb = statistics.mean(debiased_scores[key]) if debiased_scores[key] else 0.0
            delta = (deb - raw) * 100
            print(f"{key:<14}{raw:>11.1%}{deb:>14.1%}{delta:>+9.1f}%p")


if __name__ == "__main__":
    asyncio.run(main())
