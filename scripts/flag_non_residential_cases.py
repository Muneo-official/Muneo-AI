"""아파트 리모델링과 성격이 다른 사례(상가/공장 등 비주거 공간)를 찾아 is_non_residential
플래그를 세운다.

계기: 20평 수도권 쿼리에 화재 피해 상가주택 전면 철거·복구 공사(article_id 902415, 평당
396만원)가 섞여 들어가 최댓값을 크게 왜곡한 게 발견됨(2026-09-17). DB에 공간유형/건물유형
필드가 아예 없어 평수·지역·공종·자재등급만으로는 이런 케이스를 걸러낼 방법이 없었다.

키워드는 "상가|공장|근생|점포"만 쓴다 — "화재"를 포함하면 화재감지기/화재등 같은 일반
아파트 시공 품목까지 오탐되는 게 확인됨(article_id 877308). "상가"/"공장"류 키워드는
코퍼스 709건 중 9건에서만 매칭되고 전부 실제 비주거 공간 공사로 수동 확인됨.

기본은 dry-run(매칭 목록만 출력) — 실제 반영하려면 --apply.

실행:
    python -m scripts.flag_non_residential_cases            # dry-run
    python -m scripts.flag_non_residential_cases --apply     # 실제 반영
"""

import argparse
import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings

load_dotenv()

PATTERN = r"상가|공장|근생|점포"


async def main(apply: bool) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    query = {
        "is_non_residential": {"$ne": True},
        "$or": [
            {"body_text": {"$regex": PATTERN}},
            {"request_body_text": {"$regex": PATTERN}},
        ],
    }
    cursor = col.find(query, {"article_id": 1, "region": 1, "size_pyeong": 1, "cost_per_pyeong": 1})

    matched = []
    async for doc in cursor:
        matched.append(doc)
        print(f"  {doc['article_id']}  {doc.get('region')}/{doc.get('size_pyeong')}평  "
              f"평당 {doc.get('cost_per_pyeong', 0):,}원")

    if apply:
        ids = [d["_id"] for d in matched]
        if ids:
            await col.update_many({"_id": {"$in": ids}}, {"$set": {"is_non_residential": True}})

    client.close()

    mode = "실제 반영" if apply else "DRY-RUN"
    print(f"\n[{mode}] 비주거 사례 {len(matched)}건" + (" 플래그 완료" if apply else " (플래그 대상)"))
    if not apply:
        print("--apply 플래그로 실행하면 실제 DB에 반영됩니다.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="실제 DB 반영 (기본: dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
