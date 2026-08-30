"""cross-encoder 길이 편향 de-biasing — 쿼리별(n=20) 대신 전체 쿼리를 합친 전역 회귀로 재시도.

배경: eval/ce_length_debias_experiment.py에서 쿼리별로 20개 후보만 갖고 "점수 vs 길이" 선형회귀를
추정했더니 표본이 너무 작아 과적합돼서 오히려 precision이 전 지표 악화됐다(-1.7%p ~ -2.9%p).
이 스크립트는 24개 쿼리 × 20개 후보 = 약 480개(query, candidate) 쌍을 전부 모아 **하나의 전역
회귀**로 "길이가 점수에 주는 평균적인 영향"을 추정한 뒤, 그 전역 보정을 각 쿼리 안에서 적용해
재정렬한다 — 표본을 20배 늘려서 회귀 추정이 노이즈에 휘둘리지 않게 하려는 시도.
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


def _fit_linear(xs: list[float], ys: list[float]) -> tuple[float, float]:
    n = len(xs)
    mean_x = statistics.mean(xs)
    mean_y = statistics.mean(ys)
    var_x = sum((x - mean_x) ** 2 for x in xs)
    if var_x == 0 or n < 3:
        return 0.0, mean_y
    cov_xy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = cov_xy / var_x
    intercept = mean_y - slope * mean_x
    return slope, intercept


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

    per_query = []
    all_lengths: list[float] = []
    all_scores: list[float] = []

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
        ce_scores = [float(s) for s in await run_in_threadpool(reranker.predict, pairs)]
        lengths = [float(len(t)) for t in texts]

        per_query.append((ids, lengths, ce_scores, labels))
        all_lengths.extend(lengths)
        all_scores.extend(ce_scores)

    client.close()

    slope, intercept = _fit_linear(all_lengths, all_scores)
    print(f"전역 회귀: score = {slope:.6f} * length + {intercept:.4f} (n={len(all_lengths)})\n")

    raw_scores = {f"precision@{k}": [] for k in K_VALUES}
    raw_scores |= {f"recall@{k}": [] for k in K_VALUES}
    debiased_scores = {f"precision@{k}": [] for k in K_VALUES}
    debiased_scores |= {f"recall@{k}": [] for k in K_VALUES}

    for ids, lengths, ce_scores, labels in per_query:
        raw_order = sorted(range(len(ids)), key=lambda i: ce_scores[i], reverse=True)
        raw_ranked = [ids[i] for i in raw_order][:TOP_K]

        residuals = [s - (slope * length + intercept) for length, s in zip(lengths, ce_scores)]
        debiased_order = sorted(range(len(ids)), key=lambda i: residuals[i], reverse=True)
        debiased_ranked = [ids[i] for i in debiased_order][:TOP_K]

        for k in K_VALUES:
            raw_scores[f"precision@{k}"].append(_precision_at_k(raw_ranked, labels, k))
            raw_scores[f"recall@{k}"].append(_pool_recall_at_k(raw_ranked, labels, k))
            debiased_scores[f"precision@{k}"].append(_precision_at_k(debiased_ranked, labels, k))
            debiased_scores[f"recall@{k}"].append(_pool_recall_at_k(debiased_ranked, labels, k))

    print(f"{'지표':<14}{'CE 원점수':>12}{'전역 de-bias':>14}{'변화':>10}")
    for k in K_VALUES:
        for metric in ("precision", "recall"):
            key = f"{metric}@{k}"
            raw = statistics.mean(raw_scores[key]) if raw_scores[key] else 0.0
            deb = statistics.mean(debiased_scores[key]) if debiased_scores[key] else 0.0
            delta = (deb - raw) * 100
            print(f"{key:<14}{raw:>11.1%}{deb:>14.1%}{delta:>+9.1f}%p")


if __name__ == "__main__":
    asyncio.run(main())
