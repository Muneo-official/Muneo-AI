"""eval/retrieval_eval.py 변형 — BM25Okapi의 b(길이 정규화 강도)를 0.75(기본)/1.0으로 바꿔가며 비교.

배경: eval/results/reranker_hybrid_eval.md의 "원인 분석" 참고 — 하이브리드+리랭킹이 top-5/top-10에서
반대 방향으로 움직이는 원인을 추적해보니, 요청글이 긴 사례가 BM25에서 체계적으로 유리해지는 편향이
관찰됐다. 이 스크립트는 그 편향이 BM25의 길이 정규화 강도(`b`) 조정만으로 없어지는지 검증한다.

프로덕션 코드(app/domain/estimate_engine.py)는 건드리지 않고, EstimateEngine._hybrid_rerank()의
BM25 부분만 여기서 b값을 바꿔가며 재구현해 비교한다.
"""

import asyncio
import json
import pathlib
import statistics
from collections import defaultdict

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine, _tokenize

load_dotenv()

BASE = pathlib.Path(__file__).parent / "test_inputs"
K_VALUES = [5, 10]
RERANK_POOL = 20
TOP_K = 15
B_VALUES = (0.75, 1.0)  # 0.75 = rank_bm25 기본값, 1.0 = 완전 길이 정규화


def _rrf(rank_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for rank_list in rank_lists:
        for rank, doc_id in enumerate(rank_list, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return scores


async def _hybrid_rerank_variant(query: str, cases: list[dict], reranker: CrossEncoder, bm25_b: float) -> list[dict]:
    if len(cases) <= TOP_K:
        return cases
    ids = [str(c.get("article_id") or i) for i, c in enumerate(cases)]
    texts = [EstimateEngine._case_text(c) for c in cases]
    id_to_case = dict(zip(ids, cases))
    id_to_text = dict(zip(ids, texts))

    bm25 = BM25Okapi([_tokenize(t) for t in texts], b=bm25_b)
    bm25_scores = bm25.get_scores(_tokenize(query))
    bm25_rank_ids = [ids[i] for i in sorted(range(len(ids)), key=lambda i: bm25_scores[i], reverse=True)]

    rrf_scores = _rrf([ids, bm25_rank_ids])
    pool_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[: min(len(ids), RERANK_POOL)]

    pairs = [(query, id_to_text[i]) for i in pool_ids]
    ce_scores = await run_in_threadpool(reranker.predict, pairs)
    order = sorted(range(len(pool_ids)), key=lambda i: ce_scores[i], reverse=True)
    return [id_to_case[pool_ids[i]] for i in order][:TOP_K]


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


async def _run_variant(bm25_b, reranker, engine, cases_col, queries, by_query) -> dict[str, list[float]]:
    scores: dict[str, list[float]] = {f"precision@{k}": [] for k in K_VALUES}
    scores |= {f"recall@{k}": [] for k in K_VALUES}
    for query_id, rows in by_query.items():
        labels = {r["article_id"]: r["label"] for r in rows}
        vector_ranked = sorted((r for r in rows if r.get("vector_rank")), key=lambda r: r["vector_rank"])
        vector_ids = [r["article_id"] for r in vector_ranked]
        if not vector_ids:
            continue
        docs = {
            d["article_id"]: d
            async for d in cases_col.find({"article_id": {"$in": vector_ids}}, {"embedding": 0})
        }
        ordered_cases = [docs[a] for a in vector_ids if a in docs]
        query_text = engine.build_query(queries[query_id]["input"])
        reranked = await _hybrid_rerank_variant(query_text, ordered_cases, reranker, bm25_b)
        hybrid_ids = [str(c.get("article_id")) for c in reranked]
        for k in K_VALUES:
            scores[f"precision@{k}"].append(_precision_at_k(hybrid_ids, labels, k))
            scores[f"recall@{k}"].append(_pool_recall_at_k(hybrid_ids, labels, k))
    return scores


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

    for b in B_VALUES:
        scores = await _run_variant(b, reranker, engine, cases_col, queries, by_query)
        print(f"\n=== BM25 b={b} ===")
        for k in K_VALUES:
            for metric in ("precision", "recall"):
                key = f"{metric}@{k}"
                vals = scores[key]
                mean = statistics.mean(vals) if vals else 0.0
                print(f"  {key:<12}{mean:.1%}")

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
