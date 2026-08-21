"""
scripts/expire_estimates.py — valid_until이 지난 status="saved" 견적을 status="expired"로 전환한다.

`status="contracted"`(실제 계약 완료된 견적)는 대상에서 제외한다 — 만료는 "아직 참고용으로 쓸 수
있는 견적"에만 의미가 있는 개념이고, 계약까지 끝난 건 이미 실사용 데이터라 만료 처리할 이유가 없다.

cron 등으로 주기적으로(예: 하루 한 번) 돌리는 걸 전제로 한 배치 스크립트다. 여러 번 실행해도
안전하다 — 이미 expired인 문서는 필터 조건(status="saved")에 안 걸려서 다시 안 건드린다.
"""

import sys
from datetime import UTC, datetime

from dotenv import load_dotenv
from pymongo import MongoClient

from app.core.config import get_settings

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()


def main() -> None:
    settings = get_settings()
    db = MongoClient(settings.mongo_uri)[settings.mongo_db_name]

    now = datetime.now(UTC)
    result = db["estimates"].update_many(
        {"status": "saved", "valid_until": {"$lt": now}},
        {"$set": {"status": "expired"}},
    )

    print(f"[OK] {result.modified_count}건을 status=expired로 전환했습니다 "
          f"(matched={result.matched_count}, 기준 시각={now.isoformat()})")


if __name__ == "__main__":
    main()
