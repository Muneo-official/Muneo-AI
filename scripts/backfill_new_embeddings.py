"""embedding 없는 estimate_cases 문서에 벡터를 채운다.

pipeline/ingest.py의 route_and_save()는 embedding을 계산하지 않는다 — 그래서 크롤러로
새로 수집·파싱된 케이스는 estimate_cases에 저장은 되지만 $vectorSearch
(app/domain/estimate_engine.py)엔 절대 안 잡힌다. 벡터 검색 관점에서는 존재하지 않는
문서나 마찬가지다(eval/results/reranker_hybrid_eval.md "851건 추가 후 재검증 시도" 참고 —
851건을 새로 넣었는데 precision@k가 이전 문서 수치와 소수점까지 동일하게 나온 원인이 이거였다).

scripts/backfill_embeddings.py와는 다른 스크립트다 — 그건 예전 Chroma Cloud 코퍼스를
Mongo로 옮긴 일회성 마이그레이션(article_id 매칭으로 기존 임베딩을 그대로 복사)이고,
새로 파싱되는 케이스의 임베딩을 만들어주지는 않는다. 이 스크립트는 크롤링이 반복될
때마다(=embedding 없는 문서가 쌓일 때마다) 계속 재실행할 수 있다.

임베딩 대상 텍스트는 pipeline/reference/build_rag.py의 build_document()와 동일한 조합
(평수/지역/공종/요청 전문, 트렁케이션 없음)을 쓴다 — app/domain/estimate_engine.py의
_case_text()는 BM25/cross-encoder용으로 요청글을 60자로 자르는데(CE 길이 편향 완화책,
CASE_TEXT_REQUEST_CAP), 그 캡을 임베딩에도 적용하면 기존에 이미 임베딩된 557건(캡 없이
인코딩됨)과 텍스트 분포가 달라져 벡터 공간이 미묘하게 어긋난다.

실행:
    python -m scripts.backfill_new_embeddings --dry-run   # 대상 개수만 확인
    python -m scripts.backfill_new_embeddings
"""

import argparse
import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import UpdateOne
from sentence_transformers import SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import _HAS_TO_WORK, line_items_text

load_dotenv()

BATCH_SIZE = 64


def build_document(case: dict) -> str:
    """embedding 대상 텍스트 — pipeline/reference/build_rag.py의 build_document()와 동일하되,
    request_body_text가 없으면 line_items_text()(app/domain/estimate_engine.py)로 대체한다
    — 빈 텍스트만 임베딩하면 케이스가 거의 모든 쿼리에 무차별하게 걸리는 문제가 실측으로
    확인됐다(eval/results/reranker_hybrid_eval.md 참고)."""
    size = case.get("size_pyeong", "?")
    region = case.get("region", "")
    works = [name for key, name in _HAS_TO_WORK.items() if case.get(key) == "true"]
    request_text = (case.get("request_body_text") or "").strip() or line_items_text(case)
    header = " ".join(filter(None, [f"{size}평", region, " ".join(works), "리모델링"]))
    if request_text:
        return f"{header}\n요청공사: {request_text}"
    return header


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="대상 개수만 확인, 실제 백필 안 함")
    args = parser.parse_args()

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    # embedding이 아예 없는 문서뿐 아니라, request_body_text가 비어서 헤더만으로 임베딩됐을
    # 문서도 재대상이다 — line_items_text() 대체재가 새로 생겼으니 다시 인코딩해야 반영된다.
    has_embedding_ids = {
        d["_id"] async for d in col.find({"embedding": {"$exists": True}}, {"_id": 1})
    }
    all_docs = [d async for d in col.find({}, {"embedding": 0})]
    docs = [
        d for d in all_docs
        if d["_id"] not in has_embedding_ids or not (d.get("request_body_text") or "").strip()
    ]
    print(f"[INFO] 대상 문서(embedding 없음 또는 request_body_text 빈 문서): {len(docs)}건")

    if not docs:
        client.close()
        return

    if args.dry_run:
        for d in docs[:10]:
            print(f"  - {d.get('article_id')} ({d.get('region')}, {d.get('size_pyeong')}평)")
        if len(docs) > 10:
            print(f"  ... 외 {len(docs) - 10}건")
        print("[DRY-RUN] 실제 백필은 안 함")
        client.close()
        return

    embedder = SentenceTransformer(settings.embed_model)

    updated = 0
    for i in range(0, len(docs), BATCH_SIZE):
        chunk = docs[i : i + BATCH_SIZE]
        texts = [build_document(d) for d in chunk]
        embeddings = embedder.encode(texts, show_progress_bar=False)

        ops = [
            UpdateOne({"_id": d["_id"]}, {"$set": {"embedding": [float(x) for x in emb]}})
            for d, emb in zip(chunk, embeddings)
        ]
        result = await col.bulk_write(ops, ordered=False)
        updated += result.modified_count
        print(f"  진행: {min(i + BATCH_SIZE, len(docs))}/{len(docs)} (누적 {updated}건)")

    print(f"[OK] embedding 백필 완료 — {updated}건")
    client.close()


if __name__ == "__main__":
    asyncio.run(main())
