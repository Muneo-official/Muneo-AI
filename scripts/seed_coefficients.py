"""
scripts/seed_coefficients.py — correction_coefficients 컬렉션에 새 버전을 심는다.

estimate_engine.py의 DEFAULT_COEFFICIENTS(기존에 하드코딩되어 있던 값과 동일)를 그대로 사용한다 —
계수 값 자체는 바뀐 게 없고, "어디에 저장되어 있는가"만 코드에서 DB로 옮긴 것이다.

coefficient_version은 engine_version과 독립적인 축이다 — 코드가 안 바뀌어도 계수만 튜닝될 수 있고,
반대로 코드가 바뀌어도 계수는 그대로일 수 있다. 그래서 --version을 명시적으로 받는다
(예전엔 ENGINE_VERSION을 그대로 재사용했는데, 개념이 다른 두 버전을 같은 문자열로 묶어버리는
설계 실수였다).

이미 그 version이 있으면 건드리지 않는다 (덮어쓰기 방지 — 계수를 바꾸고 싶으면 새 버전을 추가할 것).

"""

import argparse
from datetime import UTC, datetime

from dotenv import load_dotenv
from pymongo import MongoClient

from app.core.config import get_settings
from app.domain.estimate_engine import DEFAULT_COEFFICIENTS

load_dotenv()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True, help="coefficient_version 문자열 (예: 1.1.0)")
    args = parser.parse_args()

    settings = get_settings()
    db = MongoClient(settings.mongo_uri)[settings.mongo_db_name]
    col = db["correction_coefficients"]

    col.create_index("version", unique=True)

    if col.find_one({"version": args.version}):
        print(f"[SKIP] version={args.version} 이미 존재함 — 건드리지 않음")
        return

    doc = {
        "version": args.version,
        "effective_from": datetime.now(UTC),
        **DEFAULT_COEFFICIENTS,
    }
    col.insert_one(doc)
    print(f"[OK] correction_coefficients에 version={args.version} 시드 완료")


if __name__ == "__main__":
    main()
