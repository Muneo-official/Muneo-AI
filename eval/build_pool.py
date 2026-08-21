"""
eval/build_pool.py — 쿼리별 라벨링 대상 pool 생성 (TREC 스타일 pooling)

eval/sample_queries.py가 만든 쿼리마다:
  - 벡터 검색(필터 없음) top-20
  - BM25(전체 코퍼스) top-20
을 합쳐(중복 제외) pool을 만들고, 규칙 기반 추정 relevance(suggested_relevant)를 같이 계산해서
CSV로 내보낸다. 사람은 이 CSV의 'label' 열만 검토/수정하면 된다 (아래 기준 참고).

relevant 판단 기준 (PORTFOLIO_UPGRADE_NOTES.md 4번):
  - 평수 ±5평
  - 동일 지역 또는 인접 권역 (서울/수도권/지방 버킷 기준)
  - 동일 공사유형 (쿼리가 요청한 공종 중 최소 1개 이상 해당 사례에도 존재)

출력:
  eval/test_inputs/pool.json   — 프로그램용 (retrieval_eval.py가 읽음)
  eval/test_inputs/labels.csv  — 사람이 라벨링할 파일 (Excel/Sheets로 열어서 label 열만 수정)

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
from app.domain.estimate_engine import _HAS_TO_WORK, REGION_MAP, EstimateEngine, _tokenize

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
POOL_JSON_PATH = pathlib.Path(__file__).parent / "test_inputs" / "pool.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"

POOL_TOP_K = 20

_REGION_TO_BUCKET = {r: b for b, rs in REGION_MAP.items() for r in rs}
_ADJACENT_BUCKETS = {
    "서울": {"서울", "수도권"},
    "수도권": {"서울", "수도권"},
    "지방": {"지방"},
}

_WORK_MAP = {"바닥": "마루", "타일": "욕실", "조명": "전기/조명", "전기": "전기/조명"}


def _case_works(case: dict) -> set[str]:
    works = {name for key, name in _HAS_TO_WORK.items() if case.get(key) == "true"}
    return {_WORK_MAP.get(w, w) for w in works}


def _suggested_relevant(query_input: dict, case: dict) -> bool:
    q_size = int(query_input.get("평수") or 0)
    c_size = int(case.get("size_pyeong") or 0)
    if abs(q_size - c_size) > 5:
        return False

    q_bucket = query_input.get("지역", "서울")
    c_bucket = _REGION_TO_BUCKET.get(case.get("region"), "지방")
    if c_bucket not in _ADJACENT_BUCKETS.get(q_bucket, {q_bucket}):
        return False

    q_works = set(query_input.get("공종", []))
    c_works = _case_works(case)
    if q_works and not (q_works & c_works):
        return False

    return True


async def main() -> None:
    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    embedder = SentenceTransformer(settings.embed_model)

    # 전체 코퍼스 (BM25 인덱스 + 후보 상세정보 조회용)
    all_cases = [c async for c in cases_col.find({}, {"embedding": 0})]
    id_to_case = {str(c.get("article_id")): c for c in all_cases}
    corpus_ids = list(id_to_case.keys())
    corpus_texts = [EstimateEngine._case_text(id_to_case[aid]) for aid in corpus_ids]
    bm25 = BM25Okapi([_tokenize(t) for t in corpus_texts])

    pool_records = []

    for q in queries:
        query_input = q["input"]
        engine_query_text = EstimateEngine(
            case_repository=None, embedder=embedder, reranker=None
        ).build_query(query_input)

        # 벡터 top-20 (필터 없이 순수 유사도만)
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

        # BM25 top-20 (전체 코퍼스 기준)
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
                "label": int(suggested),  # 사람이 검토/수정할 열 (0/1)
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
    print(f"     JSON: {POOL_JSON_PATH}")
    print(f"     CSV : {LABELS_CSV_PATH}  ← 이 파일의 'label' 열을 검토/수정하세요")


if __name__ == "__main__":
    asyncio.run(main())
