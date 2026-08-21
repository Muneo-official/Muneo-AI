"""
scripts/setup_indexes.py — 이 프로젝트가 쓰는 모든 인덱스를 한 곳에서 관리한다.

create_index는 멱등이라 몇 번을 다시 돌려도 안전하다 (이미 있으면 아무 일도 안 함).
새 컬렉션/필터 필드가 생기면 여기에 추가한다.
"""

from dotenv import load_dotenv
from pymongo import MongoClient

from app.core.config import get_settings

load_dotenv()


def main() -> None:
    settings = get_settings()
    db = MongoClient(settings.mongo_uri)[settings.mongo_db_name]

    db["estimate_cases"].create_index("article_id", unique=True)
    db["estimate_cases"].create_index("region")
    db["estimate_cases"].create_index("size_pyeong")
    print("[estimate_cases] article_id(unique) / region / size_pyeong 인덱스 확인 완료")

    db["estimates"].create_index("user_id")
    db["estimates"].create_index("created_at")
    print("[estimates] user_id / created_at 인덱스 확인 완료")

    db["estimate_feedback"].create_index("estimate_id")
    print("[estimate_feedback] estimate_id 인덱스 확인 완료")

    db["correction_coefficients"].create_index("version", unique=True)
    print("[correction_coefficients] version(unique) 인덱스 확인 완료")

    # TTL 인덱스 — expires_at에 저장된 시각이 지나면 Mongo가 자동으로 문서를 삭제한다
    # (expireAfterSeconds=0은 "필드 값 자체가 만료 시각"이라는 뜻, N초 후가 아님)
    db["pending_estimates"].create_index("expires_at", expireAfterSeconds=0)
    print("[pending_estimates] expires_at TTL 인덱스 확인 완료")


if __name__ == "__main__":
    main()
