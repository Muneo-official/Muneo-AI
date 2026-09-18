"""estimate_cases의 cost_per_pyeong 필드를 total_cost/size_pyeong로 재계산해 채운다.

pipeline/ingest.py의 finalize_record()는 ingest 시점의 size_pyeong으로 cost_per_pyeong을
한 번만 계산한다. size_pyeong이 그 이후 다른 경로로 보정된 레코드는 total_cost/size_pyeong은
정상인데 cost_per_pyeong만 예전 값(주로 0)으로 남는다 — app/domain/estimate_engine.py의
_cost_per_pyeong()을 라이브 계산으로 바꿔 응답 자체는 이미 고쳤지만, DB 필드 자체도 다른
소비자(집계 스크립트 등)를 위해 맞춰둔다.

기본은 dry-run(변경 없이 대상 건수만 출력) — 실제 반영하려면 --apply.

실행:
    python -m scripts.backfill_cost_per_pyeong            # dry-run
    python -m scripts.backfill_cost_per_pyeong --apply     # 실제 반영
"""

import argparse
import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings

load_dotenv()


async def main(apply: bool) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    fixed = 0
    cursor = col.find(
        {"total_cost": {"$gt": 0}, "size_pyeong": {"$gt": 0}},
        {"total_cost": 1, "size_pyeong": 1, "cost_per_pyeong": 1},
    )
    async for doc in cursor:
        expected = int(doc["total_cost"] / doc["size_pyeong"])
        actual = doc.get("cost_per_pyeong") or 0
        if abs(expected - actual) <= 1:
            continue

        fixed += 1
        if apply:
            await col.update_one({"_id": doc["_id"]}, {"$set": {"cost_per_pyeong": expected}})

    client.close()

    mode = "실제 반영" if apply else "DRY-RUN"
    print(f"[{mode}] cost_per_pyeong 불일치 {fixed}건" + (" 수정 완료" if apply else " (재계산 대상)"))
    if not apply:
        print("--apply 플래그로 실행하면 실제 DB에 반영됩니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제 DB 반영 (기본: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
