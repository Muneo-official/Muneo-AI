"""estimate_cases 전체에 pipeline/validators.py의 검증 규칙을 소급 적용해 리포트를 낸다.

DB는 건드리지 않는다 — 순수 진단. 실제 재분류·필드 추가 여부는 이 리포트를 보고
별도로 결정한다.

실행: python -m scripts.validate_existing_cases
"""

import asyncio
from collections import Counter

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from pipeline.validators import validate_case

load_dotenv()


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    total = 0
    no_parsed_estimate = 0
    rule_counts: Counter[str] = Counter()
    reclass_total = 0
    reclass_amount_total = 0
    confidences: list[float] = []

    async for case in col.find({}, {"embedding": 0}):
        total += 1
        parsed = case.get("parsed_estimate")
        if not parsed:
            no_parsed_estimate += 1
            continue

        result = validate_case(parsed, case.get("size_pyeong") or 0)
        confidences.append(result.confidence)
        for issue in result.issues:
            rule_counts[issue.rule] += 1
        if result.reclassification_suggestions:
            reclass_total += len(result.reclassification_suggestions)
            reclass_amount_total += sum(s["amount"] for s in result.reclassification_suggestions)

    client.close()

    print(f"전체 사례: {total}건 (parsed_estimate 없음: {no_parsed_estimate}건)\n")

    print("규칙별 위반 건수:")
    for rule, count in rule_counts.most_common():
        print(f"  {rule:<25}{count:>5}건")

    print(f"\n'도어공사→가구' 재분류 제안: line_item {reclass_total}건, 합계 {reclass_amount_total:,}원")

    if confidences:
        avg_conf = sum(confidences) / len(confidences)
        low_conf = sum(1 for c in confidences if c < 0.7)
        print(f"\n평균 신뢰도: {avg_conf:.2f}, confidence<0.7인 사례: {low_conf}건")


if __name__ == "__main__":
    asyncio.run(main())
