"""
eval/refresh_pool.py — 코퍼스에 새 사례가 추가됐을 때 pool.json/labels.csv를
사람이 검토한 기존 label을 보존하면서 갱신한다.

build_pool.py를 그냥 재실행하면 label 열이 전부 suggested_relevant로 리셋돼서
apply_review_labels.py로 반영해둔 사람 검토 결과가 통째로 날아간다(build_pool.py는
기존 pool.json을 읽지 않고 매번 덮어쓰기만 함). 이 스크립트는 (query_id, article_id)
기준으로 기존 pool.json에 있던 행은 label을 그대로 유지하고, 새로 pool에 들어온
(코퍼스 성장으로 새로 상위권에 뜬) 행만 suggested_relevant로 채워 "검토 필요" 상태로
남긴다.

extend_pool_top40.py와 목적이 다르다 — 그건 "같은 코퍼스, 더 깊은 순위(21~40위)"를
확장하는 것이고, 이건 "코퍼스 자체가 커져서 각 쿼리의 top-20이 바뀔 수 있는 경우"를
위한 것이다.

실행: python -m eval.refresh_pool
이후: python -m eval.flag_review_rows 로 검토 필요한 행만 review_priority.csv로 추출
      → label 수정 → python -m eval.apply_review_labels
"""

import asyncio
import csv
import json
import pathlib

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from rank_bm25 import BM25Okapi
from sentence_transformers import SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine, _tokenize
from eval.build_pool import POOL_TOP_K, _case_works, _suggested_relevant

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
POOL_JSON_PATH = pathlib.Path(__file__).parent / "test_inputs" / "pool.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"


async def main() -> None:
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    old_pool = json.loads(POOL_JSON_PATH.read_text(encoding="utf-8")) if POOL_JSON_PATH.exists() else []
    old_labels = {(r["query_id"], r["article_id"]): r["label"] for r in old_pool}

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    embedder = SentenceTransformer(settings.embed_model)

    all_cases = [c async for c in cases_col.find({}, {"embedding": 0})]
    id_to_case = {str(c.get("article_id")): c for c in all_cases}
    corpus_ids = list(id_to_case.keys())
    corpus_texts = [EstimateEngine._case_text(id_to_case[aid]) for aid in corpus_ids]
    bm25 = BM25Okapi([_tokenize(t) for t in corpus_texts])

    pool_records = []
    new_count = 0
    preserved_count = 0

    for q in queries:
        query_input = q["input"]
        engine_query_text = EstimateEngine(
            case_repository=None, embedder=embedder, reranker=None
        ).build_query(query_input)

        query_embedding = embedder.encode(engine_query_text).tolist()
        vector_pipeline = [
            {"$vectorSearch": {
                "index": settings.vector_index_name,
                "path": "embedding",
                "queryVector": query_embedding,
                "numCandidates": 150,
                "limit": POOL_TOP_K,
            }},
            {"$project": {"embedding": 0}},
        ]
        vector_top = [doc async for doc in cases_col.aggregate(vector_pipeline)]
        vector_ids = [str(d.get("article_id")) for d in vector_top]

        bm25_scores = bm25.get_scores(_tokenize(engine_query_text))
        bm25_order = sorted(range(len(corpus_ids)), key=lambda i: bm25_scores[i], reverse=True)[:POOL_TOP_K]
        bm25_ids = [corpus_ids[i] for i in bm25_order]

        vector_rank = {aid: r + 1 for r, aid in enumerate(vector_ids)}
        bm25_rank = {aid: r + 1 for r, aid in enumerate(bm25_ids)}

        pool_ids = list(dict.fromkeys(vector_ids + bm25_ids))  # 순서 유지 중복 제거

        for aid in pool_ids:
            case = id_to_case.get(aid)
            if case is None:
                continue
            suggested = _suggested_relevant(query_input, case)
            key = (q["query_id"], aid)
            if key in old_labels:
                label = old_labels[key]
                preserved_count += 1
            else:
                label = int(suggested)
                new_count += 1

            pool_records.append({
                "query_id": q["query_id"],
                "query_input": query_input,
                "article_id": aid,
                "region": case.get("region"),
                "size_pyeong": case.get("size_pyeong"),
                "works": sorted(_case_works(case)),
                "vector_rank": vector_rank.get(aid),
                "bm25_rank": bm25_rank.get(aid),
                "suggested_relevant": suggested,
                "label": label,
            })

    client.close()

    POOL_JSON_PATH.write_text(json.dumps(pool_records, ensure_ascii=False, indent=2), encoding="utf-8")

    with LABELS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "query_id", "query_size", "query_region", "query_works",
            "article_id", "region", "size_pyeong", "works",
            "vector_rank", "bm25_rank", "suggested_relevant", "label",
        ])
        writer.writeheader()
        for r in pool_records:
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
                "bm25_rank": r["bm25_rank"] or "",
                "suggested_relevant": int(r["suggested_relevant"]),
                "label": r["label"],
            })

    n_queries = len({r["query_id"] for r in pool_records})
    print(f"[OK] {n_queries}개 쿼리 × 총 {len(pool_records)}개 (query, case) 판단 항목")
    print(f"     기존 라벨 유지: {preserved_count}건, 신규(검토 필요): {new_count}건")
    print(f"     JSON: {POOL_JSON_PATH}")
    print(f"     CSV : {LABELS_CSV_PATH}")
    if new_count:
        print("     -> python -m eval.flag_review_rows 로 신규 포함 검토 대상 추리는 것을 권장")


if __name__ == "__main__":
    asyncio.run(main())
