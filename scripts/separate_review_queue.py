"""estimate_cases 전체를 재검증해, 신뢰도가 낮은 사례를 review_queue로 분리한다.

기본은 dry-run(변경 없이 대상만 나열) — 실제로 이관하려면 --apply.
--apply 전에는 반드시 scripts/backup_estimate_cases.py로 먼저 백업할 것.

실행:
    python -m scripts.separate_review_queue            # dry-run
    python -m scripts.separate_review_queue --apply     # 실제 이관
"""

import argparse
import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from pipeline.routing import route_case
from pipeline.validators import validate_case

load_dotenv()


async def main(apply: bool) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]
    cases_col = db["estimate_cases"]
    queue_col = db["review_queue"]

    total = 0
    to_move: list[dict] = []

    async for case in cases_col.find({}):
        total += 1
        parsed = case.get("parsed_estimate")
        if not parsed:
            continue

        result = validate_case(parsed, case.get("size_pyeong") or 0)
        if route_case(result.confidence) == "review_queue":
            case["_validation"] = {
                "confidence": result.confidence,
                "issues": [
                    {"rule": i.rule, "severity": i.severity, "message": i.message}
                    for i in result.issues
                ],
                "reclassification_suggestions": result.reclassification_suggestions,
            }
            to_move.append(case)

    mode = "실제 반영" if apply else "DRY-RUN"
    print(f"[{mode}] 전체 {total}건 중 review_queue 대상 {len(to_move)}건\n")

    for case in to_move[:20]:
        print(f"  {case.get('article_id')}  confidence={case['_validation']['confidence']:.2f}")
    if len(to_move) > 20:
        print(f"  ... 외 {len(to_move) - 20}건")

    if apply and to_move:
        await queue_col.insert_many(to_move)
        ids = [c["_id"] for c in to_move]
        result = await cases_col.delete_many({"_id": {"$in": ids}})
        print(f"\n[OK] review_queue에 {len(to_move)}건 삽입, estimate_cases에서 {result.deleted_count}건 삭제")
    elif not apply:
        print("\n--apply 플래그로 실행하면 실제 이관됩니다 (사전에 백업 필수).")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제 이관 (기본: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
