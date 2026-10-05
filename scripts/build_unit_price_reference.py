"""
scripts/build_unit_price_reference.py — 리스크 진단의 단가 기준표(unit_price_reference)를 estimate_cases에서 만든다.

    python -m scripts.build_unit_price_reference            # 무엇이 만들어지는지만 본다 (DB를 바꾸지 않는다)
    python -m scripts.build_unit_price_reference --apply    # 컬렉션을 통째로 바꾼다

코퍼스가 늘거나 재집계되면 다시 돌린다. 서버는 시작할 때 표를 읽으므로, 바꾼 뒤에는 서버를 다시 시작해야 반영된다.
표가 비어 있으면 리스크 진단은 단가 지적을 하지 않는다.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.domain.unit_price_reference import MIN_REQUESTS, build_quantity_reference, build_reference
from app.repositories.unit_price_repository import UnitPriceRepository

load_dotenv()

COLLECTION = "unit_price_reference"


async def main(apply: bool) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]
    cases = [doc async for doc in db["estimate_cases"].find(
        {}, {"parsed_estimate.line_items": 1, "request_url": 1, "article_id": 1, "is_non_residential": 1, "size_pyeong": 1})]
    table = build_reference(cases)
    quantities = build_quantity_reference(cases)
    lines = sum(len((c.get("parsed_estimate") or {}).get("line_items") or []) for c in cases)
    print(f"견적서 {len(cases)}건, 품목 {lines:,}줄 → 서로 다른 의뢰 {MIN_REQUESTS}건 이상에서 나온 품목 {len(table)}종 "
          f"(해당 줄 {sum(s['n'] for s in table.values()):,}줄)")
    for (category, name), s in sorted(table.items(), key=lambda kv: -kv[1]["n"])[:15]:
        print(f"  {category} / {name}: {s['n']}줄, 하위 10% {s['p10']:,} · 중간 {s['median']:,} · 상위 10% {s['p90']:,}")
    print(f"평당 수량을 비교할 수 있는 품목 {len(quantities)}종")
    for (category, name, unit), s in sorted(quantities.items(), key=lambda kv: -kv[1]["n"])[:10]:
        print(f"  {category} / {name} ({unit}): {s['n']}줄, 평당 중간 {s['median']:g} · 상위 10% {s['p90']:g}")
    if apply:
        count = await UnitPriceRepository(db[COLLECTION]).replace_all(table, quantities)
        print(f"[OK] {COLLECTION}에 {count}개를 넣었습니다. 서버를 다시 시작해야 반영됩니다.")
    else:
        print("[dry-run] DB를 바꾸지 않았습니다. 넣으려면 --apply")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="unit_price_reference 컬렉션을 통째로 바꾼다")
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(main(parser.parse_args().apply))
