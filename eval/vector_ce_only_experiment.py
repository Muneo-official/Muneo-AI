"""벡터 검색 top-20 → BM25/RRF 없이 cross-encoder만 재정렬 → 벡터 단독 / 하이브리드(BM25+RRF+CE)와 비교.

배경: eval/results/reranker_hybrid_eval.md에서 하이브리드(BM25+RRF+CE)가 top-5에서는 벡터 단독보다
나쁘고(-3.3%p) top-10에서는 나은(+2.9%p) 혼재된 결과가 나왔는데, 원인 분석 결과 BM25가 요청글
길이에 편향된 것으로 확인됐다(eval/bm25_b_variant_experiment.py로 b파라미터 조정은 효과 없음 확인).
그렇다면 BM25를 아예 빼고 "벡터 top-20 → cross-encoder만" 재정렬하면 더 나은지 확인한다.

프로덕션 코드(app/domain/estimate_engine.py)는 건드리지 않는다.
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
CANDIDATE_POOL = 20  # build_pool.py / retrieval_eval.py와 동일한 벡터 top-k 범위
TOP_K = 15


async def _ce_only_rerank(query: str, cases: list[dict], reranker: CrossEncoder) -> list[str]:
    """BM25/RRF 없이 벡터 후보 전체를 cross-encoder로만 재정렬."""
    ids = [str(c.get("article_id") or i) for i, c in enumerate(cases)]
    texts = [EstimateEngine._case_text(c) for c in cases]
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

    baseline = {f"precision@{k}": [] for k in K_VALUES}
    baseline |= {f"recall@{k}": [] for k in K_VALUES}
    ce_only = {f"precision@{k}": [] for k in K_VALUES}
    ce_only |= {f"recall@{k}": [] for k in K_VALUES}

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
        ce_ids = await _ce_only_rerank(query_text, ordered_cases, reranker)

        for k in K_VALUES:
            baseline[f"precision@{k}"].append(_precision_at_k(vector_ids, labels, k))
            baseline[f"recall@{k}"].append(_pool_recall_at_k(vector_ids, labels, k))
            ce_only[f"precision@{k}"].append(_precision_at_k(ce_ids, labels, k))
            ce_only[f"recall@{k}"].append(_pool_recall_at_k(ce_ids, labels, k))

    client.close()

    n = len(by_query)
    print(f"쿼리 수: {n}\n")
    print(f"{'지표':<14}{'벡터 단독':>12}{'벡터+CE(BM25 제외)':>20}{'변화':>10}")
    for k in K_VALUES:
        for metric in ("precision", "recall"):
            key = f"{metric}@{k}"
            base = statistics.mean(baseline[key]) if baseline[key] else 0.0
            ce = statistics.mean(ce_only[key]) if ce_only[key] else 0.0
            delta = (ce - base) * 100
            print(f"{key:<14}{base:>11.1%}{ce:>20.1%}{delta:>+9.1f}%p")


if __name__ == "__main__":
    asyncio.run(main())
