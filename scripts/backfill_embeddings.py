"""
scripts/backfill_embeddings.py — 일회성 마이그레이션: Chroma Cloud → MongoDB Atlas

기존 종합프로젝트 저장소의 Chroma Cloud에 있는 임베딩을, 이 저장소의 새 Atlas 클러스터로 백필한다.
  - estimates(Chroma)        → estimate_cases.embedding 필드 ($set, article_id로 매칭)
  - interior_legal_docs(Chroma) → legal_docs 컬렉션 (신규, Mongo)

Chroma는 읽기 전용으로만 접근한다 (쓰기 없음).

실행 전 준비:
  1) 이 저장소 .env에 MONGO_URI 설정 (estimate_cases에 정형 데이터가 이미 import되어 있어야 함)
  2) 실행 시점에만 Chroma 자격증명을 환경변수로 임시 제공한다.
     이 저장소 .env에는 절대 넣지 않는다 — 기존 종합프로젝트/estimate/.env 값을 그 실행 셸에서만 export:
       (PowerShell)
         $env:CHROMA_API_KEY = "..."
         $env:CHROMA_TENANT = "..."
         $env:CHROMA_DATABASE = "..."
         python scripts/backfill_embeddings.py
       (bash)
         export CHROMA_API_KEY=... CHROMA_TENANT=... CHROMA_DATABASE=...
         python scripts/backfill_embeddings.py

"""

import argparse
import os
import sys

import chromadb
from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 한글/특수문자 출력 깨짐 방지

load_dotenv()  # 이 저장소 .env → MONGO_URI만 읽는다 (CHROMA_*는 여기 없어야 정상)

ESTIMATES_COLLECTION = "estimates"
LEGAL_COLLECTION = "interior_legal_docs"
BATCH_SIZE = 200


def get_chroma_client() -> chromadb.HttpClient:
    api_key = os.environ.get("CHROMA_API_KEY")
    tenant = os.environ.get("CHROMA_TENANT")
    database = os.environ.get("CHROMA_DATABASE")

    missing = [k for k, v in {
        "CHROMA_API_KEY": api_key, "CHROMA_TENANT": tenant, "CHROMA_DATABASE": database,
    }.items() if not v]
    if missing:
        raise OSError(
            f"Chroma 자격증명 누락: {', '.join(missing)}. "
            "기존 종합프로젝트/estimate/.env 값을 이 스크립트를 실행하는 셸에만 임시로 "
            "export 해서 사용하세요 (이 저장소 .env에는 저장하지 않습니다)."
        )
    return chromadb.HttpClient(
        ssl=True,
        host="api.trychroma.com",
        tenant=tenant,
        database=database,
        headers={"x-chroma-token": api_key},
    )


def get_mongo_db():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise OSError("MONGO_URI가 .env에 없습니다.")
    db_name = os.environ.get("MONGO_DB_NAME", "estimate_db")
    return MongoClient(uri)[db_name]


def backfill_estimate_embeddings(chroma: chromadb.HttpClient, mongo_db) -> None:
    """estimates(Chroma) → estimate_cases ($set, article_id로 매칭).

    embedding뿐 아니라 Chroma metadata(build_rag.py의 build_metadata()가 계산한
    total_cost/cost_per_pyeong/material_grade/cost_*/has_* 등 파생 필드)도 함께 옮긴다.
    이 파생 필드들은 원래 Mongo estimate_cases에는 저장된 적이 없고 Chroma에만 있었다
    (article_id/region/size_pyeong 등 원본 필드만 Mongo에 있었음).
    """
    print(f"[estimates] Chroma '{ESTIMATES_COLLECTION}' → Mongo 'estimate_cases' (embedding + metadata)")
    collection = chroma.get_collection(name=ESTIMATES_COLLECTION)
    cases_col = mongo_db["estimate_cases"]

    total = collection.count()
    print(f"  Chroma 문서 수: {total}")
    if total == 0:
        print("  [SKIP] Chroma 컬렉션이 비어 있습니다.")
        return

    matched = 0
    offset = 0
    while offset < total:
        batch = collection.get(limit=BATCH_SIZE, offset=offset, include=["embeddings", "metadatas"])
        ops = []
        for article_id, embedding, metadata in zip(batch["ids"], batch["embeddings"], batch["metadatas"]):
            update = dict(metadata or {})
            update.pop("article_id", None)  # 매칭 키는 그대로 두고 갱신 대상에서 제외
            update["embedding"] = [float(x) for x in embedding]
            ops.append(UpdateOne({"article_id": article_id}, {"$set": update}))
        if ops:
            result = cases_col.bulk_write(ops, ordered=False)
            matched += result.matched_count
        offset += BATCH_SIZE
        print(f"  진행: {min(offset, total)}/{total} (누적 매칭 {matched}건)")

    unmatched = cases_col.count_documents({"embedding": {"$exists": False}})
    print(f"[estimates] 완료 — Mongo article_id 매칭 {matched}건, embedding 없는 문서 {unmatched}건 "
          f"(Chroma에 없거나 article_id 불일치 — 원인 확인 필요)")


def backfill_legal_docs(chroma: chromadb.HttpClient, mongo_db) -> None:
    """interior_legal_docs(Chroma, chunk 단위) → legal_docs(Mongo, 신규 컬렉션)."""
    print(f"[legal_docs] Chroma '{LEGAL_COLLECTION}' → Mongo 'legal_docs'")
    collection = chroma.get_collection(name=LEGAL_COLLECTION)
    legal_col = mongo_db["legal_docs"]

    total = collection.count()
    print(f"  Chroma chunk 수: {total}")
    if total == 0:
        print("  [SKIP] Chroma 컬렉션이 비어 있습니다.")
        return

    upserted = 0
    offset = 0
    while offset < total:
        batch = collection.get(
            limit=BATCH_SIZE, offset=offset,
            include=["documents", "metadatas", "embeddings"],
        )
        for chunk_id, text, metadata, embedding in zip(
            batch["ids"], batch["documents"], batch["metadatas"], batch["embeddings"]
        ):
            doc = {
                "_id": chunk_id,
                "text": text,
                "embedding": [float(x) for x in embedding],
                **(metadata or {}),
            }
            legal_col.replace_one({"_id": chunk_id}, doc, upsert=True)
            upserted += 1
        offset += BATCH_SIZE
        print(f"  진행: {min(offset, total)}/{total}")

    print(f"[legal_docs] 완료 — upsert {upserted}건")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", choices=["estimates", "legal"], default=None,
                        help="지정하지 않으면 둘 다 실행")
    parser.add_argument("--chroma-env-file", default=None,
                        help="CHROMA_API_KEY/CHROMA_TENANT/CHROMA_DATABASE가 담긴 기존 저장소의 "
                             ".env 경로 (예: 종합프로젝트/estimate/.env). 커맨드라인에 시크릿을 "
                             "직접 노출하지 않기 위한 옵션 — 이 저장소 .env와는 별개로 읽기만 한다.")
    args = parser.parse_args()

    if args.chroma_env_file:
        load_dotenv(args.chroma_env_file, override=True)

    chroma = get_chroma_client()
    mongo_db = get_mongo_db()

    if args.only in (None, "estimates"):
        backfill_estimate_embeddings(chroma, mongo_db)
    if args.only in (None, "legal"):
        backfill_legal_docs(chroma, mongo_db)


if __name__ == "__main__":
    main()
