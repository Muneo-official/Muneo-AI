"""
scripts/import_seed_data.py — 기존 종합프로젝트 저장소의 정형 데이터(JSON)를 새 Atlas 클러스터로 적재

mongoimport 바이너리 설치 없이, pymongo로 동일한 효과를 낸다.
JSON import는 인덱스를 가져오지 않으므로, 적재 후 estimate_cases.article_id에
unique index를 별도로 생성한다.
"""

import argparse
import json
import os

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv()


def get_mongo_db():
    uri = os.environ.get("MONGO_URI")
    if not uri:
        raise OSError("MONGO_URI가 .env에 없습니다.")
    db_name = os.environ.get("MONGO_DB_NAME", "estimate_db")
    return MongoClient(uri)[db_name]


def import_json_array(db, collection_name: str, file_path: str) -> None:
    with open(file_path, encoding="utf-8") as f:
        docs = json.load(f)

    if not isinstance(docs, list):
        raise ValueError(f"{file_path}는 JSON 배열이어야 합니다.")

    col = db[collection_name]
    existing = col.estimated_document_count()
    if existing:
        print(f"[{collection_name}] 이미 {existing}건 존재 — 건너뜀 (중복 적재 방지)")
        return

    if docs:
        result = col.insert_many(docs)
        print(f"[{collection_name}] {len(result.inserted_ids)}건 적재 완료 (원본 {len(docs)}건)")
    else:
        print(f"[{collection_name}] 원본 파일이 비어 있습니다.")


def ensure_indexes(db) -> None:
    db["estimate_cases"].create_index("article_id", unique=True)
    print("[estimate_cases] article_id unique index 생성 완료")

    db["estimates"].create_index("user_id")
    db["estimates"].create_index("created_at")
    print("[estimates] user_id / created_at index 생성 완료")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--estimate-cases", required=True, help="estimate_cases.json 경로")
    parser.add_argument("--estimates", required=True, help="estimates.json 경로")
    args = parser.parse_args()

    db = get_mongo_db()

    import_json_array(db, "estimate_cases", args.estimate_cases)
    import_json_array(db, "estimates", args.estimates)
    ensure_indexes(db)

    print("\n최종 카운트:")
    print(f"  estimate_cases: {db['estimate_cases'].estimated_document_count()}")
    print(f"  estimates: {db['estimates'].estimated_document_count()}")


if __name__ == "__main__":
    main()
