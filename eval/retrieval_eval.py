"""
eval/retrieval_eval.py — 벡터 검색 단독 vs 하이브리드(BM25+RRF)+cross-encoder 리랭킹 비교

eval/build_pool.py + 사람 라벨링(eval/test_inputs/pool.json의 'label')을 ground truth로 삼아
precision@k(주 지표), pool 내 recall@k(보조 지표)를 계산한다.

평가 범위에 대한 설계 결정:
  - 후보는 쿼리별 "벡터 검색 top-20"(POOL_TOP_K, build_pool.py와 동일)만 사용한다.
    production의 EstimateEngine._hybrid_rerank()도 BM25를 전체 코퍼스가 아니라
    "그 순간 벡터 검색으로 가져온 후보 집합 안에서만" 계산하므로, 애초에 벡터 후보 밖의
    문서를 새로 찾아올 수 없다 — 즉 이 평가는 production 동작을 그대로 재현한 것이다.
  - 이 범위 밖 문서(BM25로만 잡힌 문서 등)는 벡터/하이브리드 둘 다 원천적으로 못 찾으므로
    pool 내 recall의 분모(그 쿼리의 pool 전체 relevant 수)에는 포함되지만 분자에는 절대
    못 들어간다 — recall이 100%를 못 채우는 게 정상이며, 이 한계를 명시적으로 표기한다.

지표:
  - precision@k: 상위 k개 중 relevant 비율 (주 지표)
  - pool 내 recall@k: 상위 k개에서 찾은 relevant 수 / 그 쿼리 pool 전체의 relevant 수 (보조 지표,
    "전체 코퍼스 기준 recall"이 아님)

"""

import asyncio
import json
import pathlib
import statistics

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
POOL_JSON_PATH = pathlib.Path(__file__).parent / "test_inputs" / "pool.json"

K_VALUES = [5, 10]


def precision_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    top_k = ranked_ids[:k]
    if not top_k:
        return 0.0
    relevant = sum(labels.get(aid, 0) for aid in top_k)
    return relevant / len(top_k)


def pool_recall_at_k(ranked_ids: list[str], labels: dict[str, int], k: int) -> float:
    total_relevant = sum(labels.values())
    if total_relevant == 0:
        return 0.0
    found = sum(labels.get(aid, 0) for aid in ranked_ids[:k])
    return found / total_relevant


async def main() -> None:
    queries = {q["query_id"]: q for q in json.loads(QUERIES_PATH.read_text(encoding="utf-8"))}
    pool = json.loads(POOL_JSON_PATH.read_text(encoding="utf-8"))

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    # 여기서는 _hybrid_rerank()만 호출한다 — 이 메서드는 case_repository/embedder를 쓰지 않는다
    # (BM25는 후보 텍스트로, cross-encoder는 쿼리 문자열로 바로 계산하기 때문)
    engine = EstimateEngine(case_repository=None, embedder=None, reranker=reranker)

    # query_id별로 pool 행 묶기
    by_query: dict[str, list[dict]] = {}
    for row in pool:
        by_query.setdefault(row["query_id"], []).append(row)

    baseline_scores = {f"precision@{k}": [] for k in K_VALUES}
    baseline_scores |= {f"recall@{k}": [] for k in K_VALUES}
    hybrid_scores = {f"precision@{k}": [] for k in K_VALUES}
    hybrid_scores |= {f"recall@{k}": [] for k in K_VALUES}

    for query_id, rows in by_query.items():
        labels = {r["article_id"]: r["label"] for r in rows}

        # baseline: 벡터 검색 순위 그대로 (vector_rank가 있는 것만, 오름차순)
        vector_ranked = sorted(
            (r for r in rows if r.get("vector_rank")),
            key=lambda r: r["vector_rank"],
        )
        vector_ids = [r["article_id"] for r in vector_ranked]

        if not vector_ids:
            continue

        # hybrid: 같은 벡터 후보 집합을 _hybrid_rerank()로 재정렬
        article_ids = vector_ids
        docs = {
            d["article_id"]: d
            async for d in cases_col.find({"article_id": {"$in": article_ids}}, {"embedding": 0})
        }
        ordered_cases = [docs[aid] for aid in article_ids if aid in docs]

        query_text = engine.build_query(queries[query_id]["input"])
        reranked_cases = await engine._hybrid_rerank(query_text, ordered_cases)
        hybrid_ids = [str(c.get("article_id")) for c in reranked_cases]

        for k in K_VALUES:
            baseline_scores[f"precision@{k}"].append(precision_at_k(vector_ids, labels, k))
            baseline_scores[f"recall@{k}"].append(pool_recall_at_k(vector_ids, labels, k))
            hybrid_scores[f"precision@{k}"].append(precision_at_k(hybrid_ids, labels, k))
            hybrid_scores[f"recall@{k}"].append(pool_recall_at_k(hybrid_ids, labels, k))

    client.close()

    n = len(by_query)
    print(f"쿼리 수: {n}\n")
    print(f"{'지표':<14}{'벡터 단독':>12}{'하이브리드+리랭킹':>18}{'변화':>10}")
    for k in K_VALUES:
        for metric in ("precision", "recall"):
            key = f"{metric}@{k}"
            base = statistics.mean(baseline_scores[key]) if baseline_scores[key] else 0.0
            hyb = statistics.mean(hybrid_scores[key]) if hybrid_scores[key] else 0.0
            delta = (hyb - base) * 100
            print(f"{key:<14}{base:>11.1%}{hyb:>18.1%}{delta:>+9.1f}%p")


if __name__ == "__main__":
    asyncio.run(main())
