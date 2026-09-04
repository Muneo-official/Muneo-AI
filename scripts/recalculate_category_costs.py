"""pipeline/categories.py의 수정된 CATEGORY_NORM으로 estimate_cases의 cost_* 필드를
재계산한다.

기본은 dry-run(변경 없이 diff만 출력) — 실제 DB에 반영하려면 --apply.

실행:
    python -m scripts.recalculate_category_costs            # dry-run
    python -m scripts.recalculate_category_costs --apply    # 실제 반영
"""

import argparse
import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from pipeline.aggregation import build_category_costs

load_dotenv()

COST_PER_PYEONG = "cost_per_pyeong"  # category 집계 대상이 아니라 total_cost/size_pyeong 유래이므로 제외


async def main(apply: bool) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    changed_docs = 0
    unchanged_docs = 0
    field_diff_totals: dict[str, int] = {}
    newly_populated: dict[str, int] = {}

    async for case in col.find({}, {"embedding": 0}):
        parsed = case.get("parsed_estimate") or {}
        line_items = parsed.get("line_items", [])
        total_cost = int(parsed.get("total_cost") or 0)
        if not line_items:
            continue

        existing_costs = {
            k: v for k, v in case.items()
            if k.startswith("cost_") and k != COST_PER_PYEONG
        }
        new_costs = build_category_costs(line_items, total_cost)

        to_unset = sorted(set(existing_costs) - set(new_costs))
        changed = {
            k: (existing_costs.get(k, 0), v)
            for k, v in new_costs.items()
            if existing_costs.get(k, 0) != v
        }

        if not changed and not to_unset:
            unchanged_docs += 1
            continue

        changed_docs += 1
        for k, (old, new) in changed.items():
            field_diff_totals[k] = field_diff_totals.get(k, 0) + abs(new - old)
            if old == 0 and new > 0:
                newly_populated[k] = newly_populated.get(k, 0) + 1

        if apply:
            update: dict = {"$set": new_costs}
            if to_unset:
                update["$unset"] = {k: "" for k in to_unset}
            await col.update_one({"_id": case["_id"]}, update)

    client.close()

    mode = "실제 반영" if apply else "DRY-RUN"
    print(f"[{mode}] 변경 대상 {changed_docs}건, 변경 없음 {unchanged_docs}건\n")

    print("필드별 변화량 합계 (|new - old|):")
    for field, total in sorted(field_diff_totals.items(), key=lambda x: -x[1]):
        new_count = newly_populated.get(field, 0)
        print(f"  {field:<20}{total:>15,}원   (신규로 값이 생긴 사례 {new_count}건)")

    if not apply:
        print("\n--apply 플래그로 실행하면 실제 DB에 반영됩니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제 DB 반영 (기본: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
