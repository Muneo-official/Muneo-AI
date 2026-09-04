"""eval/build_pool.py가 만든 pool.json(벡터 top-20 + BM25 top-20)을 벡터 top-40까지 확장한다.

배경: eval/results/reranker_hybrid_eval.md에서 BM25의 진짜 효과가 검증되지 않았다는 게 확인됐다
— 프로덕션은 벡터 후보 40개(vector_candidate_pool=40) 중 BM25+RRF로 20개(RERANK_POOL)를
추리는데, 기존 pool.json은 애초에 벡터 top-20만 담고 있어서 RRF가 걸러낼 대상 자체가 없었다.
이 스크립트는 각 쿼리의 벡터 검색을 top-40까지 다시 실행해서, 기존 pool에 없던 21~40위 후보만
새로 추려 규칙 기반 suggested_relevant를 계산하고 pool.json/labels.csv에 추가한다.

기존 1~20위 라벨(사람이 검토 완료한 것)은 건드리지 않는다 — 새로 추가되는 21~40위 행만
"사람 검토 전" 상태(label = suggested_relevant 그대로)로 남기고, 검토가 필요한 행은
eval/flag_review_rows.py를 다시 돌리면 이 확장분도 같이 걸러진다.
"""

import asyncio
import csv
import json
import pathlib

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine
from eval.build_pool import _case_works, _suggested_relevant

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
POOL_JSON_PATH = pathlib.Path(__file__).parent / "test_inputs" / "pool.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"

EXTENDED_TOP_K = 40  # app.core.config.Settings.vector_candidate_pool(프로덕션 기본값)과 동일


async def main() -> None:
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    pool = json.loads(POOL_JSON_PATH.read_text(encoding="utf-8"))
    existing_keys = {(r["query_id"], r["article_id"]) for r in pool}

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    embedder = SentenceTransformer(settings.embed_model)
    engine = EstimateEngine(case_repository=None, embedder=embedder, reranker=None)

    new_rows = []

    for q in queries:
        query_input = q["input"]
        query_text = engine.build_query(query_input)
        query_embedding = embedder.encode(query_text).tolist()

        vector_pipeline = [
            {"$vectorSearch": {
                "index": settings.vector_index_name,
                "path": "embedding",
                "queryVector": query_embedding,
                "numCandidates": 150,
                "limit": EXTENDED_TOP_K,
            }},
            {"$project": {"embedding": 0}},
        ]
        vector_top = [doc async for doc in cases_col.aggregate(vector_pipeline)]

        for rank, case in enumerate(vector_top, start=1):
            aid = str(case.get("article_id"))
            key = (q["query_id"], aid)
            if key in existing_keys:
                continue  # 이미 기존 pool(top-20 또는 BM25 풀)에 있음 — 라벨 유지, 건드리지 않음
            if rank <= 20:
                continue  # top-20 범위는 build_pool.py가 이미 다뤘어야 함(방어적으로 재확인만)

            suggested = _suggested_relevant(query_input, case)
            new_rows.append({
                "query_id": q["query_id"],
                "query_input": query_input,
                "article_id": aid,
                "region": case.get("region"),
                "size_pyeong": case.get("size_pyeong"),
                "works": sorted(_case_works(case)),
                "vector_rank": rank,
                "bm25_rank": None,
                "suggested_relevant": suggested,
                "label": int(suggested),
            })
            existing_keys.add(key)

    client.close()

    pool.extend(new_rows)
    POOL_JSON_PATH.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    # labels.csv에도 신규 행 추가 (기존 행 포맷과 동일하게)
    with LABELS_CSV_PATH.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "query_id", "query_size", "query_region", "query_works",
            "article_id", "region", "size_pyeong", "works",
            "vector_rank", "bm25_rank", "suggested_relevant", "label",
        ])
        for r in new_rows:
            qi = r["query_input"]
            writer.writerow({
                "query_id": r["query_id"],
                "query_size": qi.get("평수"),
                "query_region": qi.get("지역"),
                "query_works": ",".join(qi.get("공종", [])),
                "article_id": r["article_id"],
                "region": r["region"],
                "size_pyeong": r["size_pyeong"],
                "works": ",".join(r["works"]),
                "vector_rank": r["vector_rank"] or "",
                "bm25_rank": "",
                "suggested_relevant": int(r["suggested_relevant"]),
                "label": r["label"],
            })

    n_relevant = sum(r["label"] for r in new_rows)
    print(f"[OK] 신규 21~{EXTENDED_TOP_K}위 후보 {len(new_rows)}건 추가 (규칙 기반 relevant {n_relevant}건)")
    print("     -> eval/flag_review_rows.py를 다시 실행해 검토 필요 행을 추려낼 것")


if __name__ == "__main__":
    asyncio.run(main())
