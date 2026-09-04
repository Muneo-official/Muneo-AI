"""BM25의 진짜 효과 검증 — 벡터 후보를 프로덕션과 동일하게 top-40으로 넓혀서 재평가.

배경: eval/retrieval_eval.py / eval/vector_ce_only_experiment.py는 eval/test_inputs/pool.json이
원래 벡터 top-20만 담고 있어서, 프로덕션의 RERANK_POOL(20)과 후보 풀 크기가 같아 BM25/RRF가
아무것도 걸러내지 못하는 구조적 한계가 있었다(eval/results/reranker_hybrid_eval.md "정정" 절).
eval/extend_pool_top40.py로 pool을 벡터 top-40까지 확장(21~40위 456건, 규칙 기반 라벨 — 검토
필요 140건은 사람 재검토 없이 suggested_relevant를 그대로 신뢰함, 노이즈 가능성 있음)한 뒤,
이 스크립트는 `EstimateEngine._hybrid_rerank()`를 프로덕션과 동일한 설정(RERANK_POOL=20)으로
직접 호출해 벡터 top-40을 실제로 20개로 걸러내는 BM25/RRF 효과를 측정한다.

비교 대상:
  - baseline: 벡터 검색 순위 그대로 top-40 중 상위 k개
  - hybrid: EstimateEngine._hybrid_rerank()를 그대로 호출(BM25+RRF로 40→20 필터링 후 cross-encoder)
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

BASE = pathlib.Path(__file__).parent / "test_inputs"
K_VALUES = [5, 10]


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

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    # 프로덕션과 동일하게 RERANK_POOL=20이 실제로 필터링 역할을 하도록, 후보를 40개 그대로 넣는다.
    engine = EstimateEngine(case_repository=None, embedder=None, reranker=reranker)

    by_query: dict[str, list[dict]] = {}
    for row in pool:
        by_query.setdefault(row["query_id"], []).append(row)

    baseline = {f"precision@{k}": [] for k in K_VALUES}
    baseline |= {f"recall@{k}": [] for k in K_VALUES}
    hybrid = {f"precision@{k}": [] for k in K_VALUES}
    hybrid |= {f"recall@{k}": [] for k in K_VALUES}

    n = 0
    for query_id, rows in by_query.items():
        labels = {r["article_id"]: r["label"] for r in rows}
        vector_ranked = sorted((r for r in rows if r.get("vector_rank")), key=lambda r: r["vector_rank"])
        vector_ids = [r["article_id"] for r in vector_ranked]  # 이제 최대 40개
        if len(vector_ids) < 21:
            print(f"[skip] {query_id}: 벡터 후보가 {len(vector_ids)}개뿐 (40 확장 실패, 건너뜀)")
            continue
        n += 1

        docs = {
            d["article_id"]: d
            async for d in cases_col.find({"article_id": {"$in": vector_ids}}, {"embedding": 0})
        }
        ordered_cases = [docs[a] for a in vector_ids if a in docs]

        query_text = engine.build_query(queries[query_id]["input"])
        reranked_cases = await engine._hybrid_rerank(query_text, ordered_cases)
        hybrid_ids = [str(c.get("article_id")) for c in reranked_cases]

        for k in K_VALUES:
            baseline[f"precision@{k}"].append(_precision_at_k(vector_ids, labels, k))
            baseline[f"recall@{k}"].append(_pool_recall_at_k(vector_ids, labels, k))
            hybrid[f"precision@{k}"].append(_precision_at_k(hybrid_ids, labels, k))
            hybrid[f"recall@{k}"].append(_pool_recall_at_k(hybrid_ids, labels, k))

    client.close()

    print(f"\n쿼리 수(top-40 확장 성공): {n}\n")
    print(f"{'지표':<14}{'벡터 단독(top-40)':>18}{'하이브리드(40→20→CE)':>22}{'변화':>10}")
    for k in K_VALUES:
        for metric in ("precision", "recall"):
            key = f"{metric}@{k}"
            base = statistics.mean(baseline[key]) if baseline[key] else 0.0
            hyb = statistics.mean(hybrid[key]) if hybrid[key] else 0.0
            delta = (hyb - base) * 100
            print(f"{key:<14}{base:>17.1%}{hyb:>22.1%}{delta:>+9.1f}%p")


if __name__ == "__main__":
    asyncio.run(main())
