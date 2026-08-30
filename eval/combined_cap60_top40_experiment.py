"""캡=60(완화책 1) + 진짜 40→20 BM25/RRF 필터링(완화책 2 검증)을 함께 적용했을 때 효과.

배경: 두 실험을 각각 독립적으로 확인했다 —
  - eval/case_text_cap_experiment.py: _case_text() 트렁케이션을 300→60으로 줄이면
    (단, 이 실험은 BM25/RRF가 무의미했던 top-20 전용 pool에서 진행됨) 전 지표 소폭 개선
  - eval/retrieval_eval_top40.py: pool을 벡터 top-40으로 확장해 진짜 40→20 필터링을 재현하니
    (단, 이 실험은 캡=300 그대로) precision@10이 더 크게 개선됨(+4.2%p)
둘을 합치면 어떻게 되는지는 아직 확인 안 됐다 — 이 스크립트가 그 조합을 테스트한다.

프로덕션 코드(app/domain/estimate_engine.py)는 건드리지 않는다 — RRF/BM25/CE 파이프라인을
캡 파라미터만 받도록 로컬로 재구현.
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
from app.domain.estimate_engine import _HAS_TO_WORK, EstimateEngine, _tokenize

load_dotenv()

BASE = pathlib.Path(__file__).parent / "test_inputs"
K_VALUES = [5, 10]
RERANK_POOL = 20  # app/domain/estimate_engine.py의 RERANK_POOL과 동일
TOP_K = 15
CAPS_TO_TEST = (300, 60)  # 300 = 현재 프로덕션(baseline), 60 = 앞서 검증된 완화책


def _case_text_with_cap(case: dict, cap: int) -> str:
    size = case.get("size_pyeong", "?")
    region = case.get("region", "")
    works = [name for key, name in _HAS_TO_WORK.items() if case.get(key) == "true"]
    request_text = (case.get("request_body_text") or "").strip()
    header = " ".join(filter(None, [f"{size}평", region, " ".join(works), "리모델링"]))
    if request_text:
        return f"{header}\n{request_text[:cap]}"
    return header


def _rrf(rank_lists: list[list[str]], k: int = 60) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    for rank_list in rank_lists:
        for rank, doc_id in enumerate(rank_list, start=1):
            scores[doc_id] += 1.0 / (k + rank)
    return scores


async def _hybrid_rerank_with_cap(
    query: str, cases: list[dict], reranker: CrossEncoder, cap: int
) -> list[str]:
    """app/domain/estimate_engine.py의 EstimateEngine._hybrid_rerank()와 동일한 흐름
    (벡터 순위 그대로 → BM25+RRF로 RERANK_POOL개로 필터 → cross-encoder 재정렬),
    단 _case_text()의 트렁케이션 길이만 cap으로 바꿔서 재현."""
    if len(cases) <= TOP_K:
        return [str(c.get("article_id")) for c in cases]

    ids = [str(c.get("article_id") or i) for i, c in enumerate(cases)]
    texts = [_case_text_with_cap(c, cap) for c in cases]
    id_to_text = dict(zip(ids, texts))

    bm25 = BM25Okapi([_tokenize(t) for t in texts])
    bm25_scores = bm25.get_scores(_tokenize(query))
    bm25_rank_ids = [ids[i] for i in sorted(range(len(ids)), key=lambda i: bm25_scores[i], reverse=True)]

    rrf_scores = _rrf([ids, bm25_rank_ids])
    pool_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[: min(len(ids), RERANK_POOL)]

    pairs = [(query, id_to_text[i]) for i in pool_ids]
    ce_scores = await run_in_threadpool(reranker.predict, pairs)
    order = sorted(range(len(pool_ids)), key=lambda i: ce_scores[i], reverse=True)
    return [pool_ids[i] for i in order][:TOP_K]


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
        vector_ids = [r["article_id"] for r in vector_ranked]
        if len(vector_ids) < 21:
            continue
        docs = {
            d["article_id"]: d
            async for d in cases_col.find({"article_id": {"$in": vector_ids}}, {"embedding": 0})
        }
        ordered_cases = [docs[a] for a in vector_ids if a in docs]
        query_text = engine.build_query(queries[query_id]["input"])
        prepared.append((query_text, ordered_cases, labels))

    print(f"쿼리 수(top-40 확장 성공): {len(prepared)}\n")
    print(f"{'cap':>6}{'precision@5':>14}{'recall@5':>12}{'precision@10':>15}{'recall@10':>12}")
    for cap in CAPS_TO_TEST:
        scores = {f"precision@{k}": [] for k in K_VALUES}
        scores |= {f"recall@{k}": [] for k in K_VALUES}
        for query_text, ordered_cases, labels in prepared:
            ranked_ids = await _hybrid_rerank_with_cap(query_text, ordered_cases, reranker, cap)
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
