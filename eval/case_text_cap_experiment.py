"""_case_text()의 request_body_text 트렁케이션 길이(현재 300자)를 줄이면 cross-encoder 길이
편향이 실제로 줄어들고 precision이 개선되는지 확인하는 실험.

배경: eval/results/reranker_hybrid_eval.md "확정" 절에서 cross-encoder가 텍스트 길이 자체에
편향된다는 게 필러 패딩 통제 실험으로 확인됐다. 실제 프로덕션 case_text 길이를 측정해보니
(밀려난 사례 199~303자 vs 끌어올려진 사례 전부 336자 — 300자 캡에 걸려 동일) 캡 300자가
"짧게 쓴 사례"와 "길게 쓴 사례"의 격차를 줄이지 못하고 있었다. 캡을 훨씬 작게(예: 100자) 잡으면
캡에 걸리는 사례가 늘어나 길이가 자연스럽게 좁혀질 것이라는 가설을 검증한다.

프로덕션 코드(app/domain/estimate_engine.py)는 건드리지 않는다 — _case_text()를 캡 길이만
파라미터화해서 로컬로 재구현.
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
from app.domain.estimate_engine import _HAS_TO_WORK, EstimateEngine

load_dotenv()

BASE = pathlib.Path(__file__).parent / "test_inputs"
K_VALUES = [5, 10]
CANDIDATE_POOL = 20
TOP_K = 15
CAPS_TO_TEST = (300, 150, 100, 60)  # 300 = 현재 프로덕션 값(baseline)


def _case_text_with_cap(case: dict, cap: int) -> str:
    """app/domain/estimate_engine.py의 EstimateEngine._case_text()와 동일하되, 트렁케이션
    길이만 인자로 받는다."""
    size = case.get("size_pyeong", "?")
    region = case.get("region", "")
    works = [name for key, name in _HAS_TO_WORK.items() if case.get(key) == "true"]
    request_text = (case.get("request_body_text") or "").strip()
    header = " ".join(filter(None, [f"{size}평", region, " ".join(works), "리모델링"]))
    if request_text:
        return f"{header}\n{request_text[:cap]}"
    return header


async def _ce_rerank_with_cap(query: str, cases: list[dict], reranker: CrossEncoder, cap: int) -> list[str]:
    ids = [str(c.get("article_id") or i) for i, c in enumerate(cases)]
    texts = [_case_text_with_cap(c, cap) for c in cases]
    pairs = [(query, t) for t in texts]
    ce_scores = await run_in_threadpool(reranker.predict, pairs)
    order = sorted(range(len(ids)), key=lambda i: ce_scores[i], reverse=True)
    return [ids[i] for i in order][:TOP_K]


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

    prepared = []
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
        query_text = engine.build_query(queries[query_id]["input"])
        prepared.append((query_text, ordered_cases, labels))

    print(f"쿼리 수: {len(prepared)}\n")
    print(f"{'cap':>6}{'precision@5':>14}{'recall@5':>12}{'precision@10':>15}{'recall@10':>12}")
    for cap in CAPS_TO_TEST:
        scores = {f"precision@{k}": [] for k in K_VALUES}
        scores |= {f"recall@{k}": [] for k in K_VALUES}
        for query_text, ordered_cases, labels in prepared:
            ranked_ids = await _ce_rerank_with_cap(query_text, ordered_cases, reranker, cap)
            for k in K_VALUES:
                scores[f"precision@{k}"].append(_precision_at_k(ranked_ids, labels, k))
                scores[f"recall@{k}"].append(_pool_recall_at_k(ranked_ids, labels, k))
        p5 = statistics.mean(scores["precision@5"])
        r5 = statistics.mean(scores["recall@5"])
        p10 = statistics.mean(scores["precision@10"])
        r10 = statistics.mean(scores["recall@10"])
        print(f"{cap:>6}{p5:>14.1%}{r5:>12.1%}{p10:>15.1%}{r10:>12.1%}")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
