"""리스크 진단의 가격 이상 탐지 — estimate_cases 코퍼스 기반.

기존 risk_analyzer.py(누락/중복/불분명)는 항목이 "있는지"만 보고 금액이 적정한지는
전혀 안 본다. 여기서는 EstimateEngine이 이미 갖고 있는 유사 사례 검색(retrieve_cases)을
그대로 재사용해, 파싱된 line_item 합산 금액이 비슷한 사례들의 가격대를 벗어나면 이슈로
추가한다. 새 카테고리 체계를 만들지 않는다 — pipeline.categories의 14개 정규화
카테고리(파서가 이미 강제)와 estimate_cases의 cost_<카테고리> 필드를 그대로 쓴다.

가격 범위는 EstimateEngine.cost_range()(P25~P75)를 재사용하지 않고 이 모듈 전용으로
_price_range()(P10~P90)를 따로 둔다 — 실측(4건의 실제 크롤링 견적서로 end-to-end 테스트)
결과, P25~P75는 "정상 범위"가 통계적으로 딱 중간 50%만 커버해서 카테고리 하나당 벗어날
확률이 이미 ~50%다. 이 서비스가 카테고리를 8~12개씩 독립적으로 체크하니 "적어도 하나는
벗어날 확률"이 1-0.5^8 ≈ 99.6%로 치솟아, 사실상 모든 견적서가 가격 이상 판정을 몇 건씩
달고 나왔다 — cost_range() 자체를 넓히면 generate()의 실제 가견적 산출(정확도가 생명)에
영향을 주므로, 체크 전용으로 별도 함수를 분리했다.
"""

import statistics
from collections import defaultdict
from typing import Any

from app.core.logging import log_event
from app.domain.estimate_engine import REGION_MAP, EstimateEngine
from app.domain.risk_models import RiskIssue
from app.schemas.risk import AnalyzeRiskCommand
from pipeline.crawl_region import REGION_PATTERNS

MIN_COMPARABLE_CASES = 3   # retrieve_cases 결과가 이보다 적으면 근거 부족으로 가격 체크 스킵
MIN_COMPARABLE_VALUES = 3  # 카테고리별 cost_* 표본이 이보다 적으면 그 카테고리는 스킵
PERCENTILE_RANGE = 10      # P10~P90 — 표본이 10개 미만이면 인덱스가 0/n-1로 접혀 사실상 min~max

# pipeline.categories.NORMALIZED_CATEGORIES(파서가 강제하는 14개) -> risk_analyzer.py가
# 아는 process 라벨. risk_analyzer는 11개 process만 알아서(창호/필름/공과잡비/확장 없음),
# 그 4개는 원래 라벨을 그대로 process로 써도 formatter가 알아서 처리한다
# (PROCESS_DISPLAY_NAME.get(process, process) — 못 찾으면 raw 라벨 그대로 표시).
_CATEGORY_TO_PROCESS = {
    "철거": "철거",
    "설비": "설비",
    "전기": "전기/조명",
    "목공": "목공",
    "도배": "도배",
    "바닥": "바닥",
    "타일": "타일",
    "욕실": "욕실",
    "도장": "도장",
    "가구": "가구",
    "창호": "창호",
    "필름": "필름",
    "공과잡비": "공과잡비",
    "확장": "확장",
}

# REGION_PATTERNS(raw 지역명 17개) -> REGION_MAP(서울/수도권/지방) 역매핑.
_RAW_REGION_TO_BUCKET = {raw: bucket for bucket, raws in REGION_MAP.items() for raw in raws}


def _normalize_region(raw_region: str) -> str:
    """자유 텍스트 지역명을 EstimateEngine이 기대하는 서울/수도권/지방 버킷으로 변환.

    REGION_PATTERNS로 raw 지역명(서울/경기/부산 등)을 먼저 찾고, REGION_MAP으로
    그 raw 지역명을 3개 버킷 중 하나로 접는다. 둘 다 새로 만들지 않고 기존 상수를
    그대로 조합만 한다.
    """
    text = raw_region or ""
    for region, keywords in REGION_PATTERNS:
        if any(kw in text for kw in keywords):
            return _RAW_REGION_TO_BUCKET.get(region, "지방")
    return "서울"  # estimate_engine의 기본값과 동일


def _price_range(values: list[int]) -> dict[str, int] | None:
    """P10~P90 기반 범위. EstimateEngine.cost_range()(P25~P75)와 계산 형태는 같지만
    퍼센타일을 넓혀 오탐(정상 견적이 우연히 범위 밖으로 잡히는 경우)을 줄인다."""
    if not values:
        return None
    s = sorted(values)
    n = len(s)
    mid = int(statistics.median(s))
    lo = s[n // PERCENTILE_RANGE]
    hi = s[-(n // PERCENTILE_RANGE) - 1]
    return {"최소": lo, "최대": hi, "중간": mid}


def _sum_amount_by_category(line_items: list[dict[str, Any]]) -> dict[str, int]:
    totals: dict[str, int] = defaultdict(int)
    for item in line_items:
        category = item.get("category")
        amount = item.get("amount")
        if category and amount:
            totals[category] += int(amount)
    return dict(totals)


def _build_engine_input(command: AnalyzeRiskCommand) -> dict:
    return {
        "평수": command.pyeong,
        "지역": _normalize_region(command.region),
        "공간유형": command.space_type,
        "건물연식": command.building_age,
        "자재등급": "중급",  # risk_detector 요청엔 자재등급 입력이 없음 — 중간값으로 가정
        "시공범위": "부분",  # 견적서에 실제로 있는 항목만 비교 대상이라 "부분" 취급이 더 안전
        "공종": [],           # risk_detector엔 사용자가 고른 공종 어휘가 없어 has_* 필터는 스킵
    }


async def check_price_anomalies(
    command: AnalyzeRiskCommand,
    line_items: list[dict[str, Any]],
    engine: EstimateEngine,
) -> list[RiskIssue]:
    """카테고리별 line_item 합산 금액을 유사 사례 가격 범위(P10~P90)와 비교해 이상 항목을 찾는다."""
    category_amounts = _sum_amount_by_category(line_items)
    if not category_amounts:
        return []

    inp = _build_engine_input(command)
    query = engine.build_query(inp)
    cases = await engine.retrieve_cases(query, inp)

    if len(cases) < MIN_COMPARABLE_CASES:
        log_event(
            "risk_price_check_skipped",
            level="info",
            reason="not_enough_comparable_cases",
            case_count=len(cases),
        )
        return []

    issues: list[RiskIssue] = []
    for category, amount in category_amounts.items():
        values = [
            int(v) for c in cases if (v := c.get(f"cost_{category}"))
        ]
        if len(values) < MIN_COMPARABLE_VALUES:
            continue

        price_range = _price_range(values)
        if price_range is None:
            continue

        if amount < price_range["최소"] or amount > price_range["최대"]:
            process = _CATEGORY_TO_PROCESS.get(category, category)
            issues.append(
                RiskIssue(
                    "불분명",
                    process,
                    f"{category} 항목 가격이 시세 범위를 벗어남",
                    f"견적 금액 {amount:,}원이 유사 사례 시세 범위"
                    f"({price_range['최소']:,}~{price_range['최대']:,}원, "
                    f"중간값 {price_range['중간']:,}원)를 벗어납니다.",
                    "업체에 해당 항목 견적 산정 근거(자재 등급, 시공 범위 등)를 확인하세요.",
                )
            )

    return issues
