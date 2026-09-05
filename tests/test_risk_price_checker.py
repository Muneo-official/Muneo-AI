from unittest.mock import AsyncMock, MagicMock

import pytest

from app.domain.risk_price_checker import _normalize_region, check_price_anomalies
from app.schemas.risk import AnalyzeRiskCommand


def _command(**overrides) -> AnalyzeRiskCommand:
    fields = dict(
        space_type="아파트",
        pyeong=30,
        room_count=3,
        floor=8,
        elevator=True,
        region="서울",
        building_age="20년이상",
        company_name="홍길동 인테리어",
        image_files=[],
    )
    fields.update(overrides)
    return AnalyzeRiskCommand(**fields)


def _mock_engine(cases: list[dict]) -> MagicMock:
    engine = MagicMock()
    engine.build_query.return_value = "30평 서울 아파트 리모델링"
    engine.retrieve_cases = AsyncMock(return_value=cases)
    return engine


def test_normalize_region_maps_raw_region_to_bucket():
    assert _normalize_region("서울 강남구") == "서울"
    assert _normalize_region("경기도 성남시") == "수도권"
    assert _normalize_region("부산광역시") == "지방"


def test_normalize_region_unrecognized_falls_back_to_seoul():
    assert _normalize_region("존재하지않는지역") == "서울"


@pytest.mark.asyncio
async def test_no_line_items_returns_no_issues():
    engine = _mock_engine(cases=[])
    issues = await check_price_anomalies(_command(), [], engine)
    assert issues == []
    engine.retrieve_cases.assert_not_called()


@pytest.mark.asyncio
async def test_too_few_comparable_cases_skips_check():
    engine = _mock_engine(cases=[{"cost_도배": 1_000_000}, {"cost_도배": 1_200_000}])
    line_items = [{"category": "도배", "description": "실크벽지", "amount": 5_000_000}]

    issues = await check_price_anomalies(_command(), line_items, engine)

    assert issues == []


@pytest.mark.asyncio
async def test_amount_outside_iqr_range_flagged():
    cases = [
        {"cost_도배": 1_000_000},
        {"cost_도배": 1_100_000},
        {"cost_도배": 1_200_000},
        {"cost_도배": 1_300_000},
    ]
    engine = _mock_engine(cases=cases)
    # 유사 사례 시세는 100만원대인데 견적서엔 500만원 -> 명백히 범위 밖
    line_items = [{"category": "도배", "description": "실크벽지", "amount": 5_000_000}]

    issues = await check_price_anomalies(_command(), line_items, engine)

    assert len(issues) == 1
    assert issues[0].process == "도배"
    assert issues[0].type == "불분명"
    assert "시세 범위" in issues[0].detail


@pytest.mark.asyncio
async def test_amount_within_range_not_flagged():
    cases = [
        {"cost_도배": 1_000_000},
        {"cost_도배": 1_100_000},
        {"cost_도배": 1_200_000},
        {"cost_도배": 1_300_000},
    ]
    engine = _mock_engine(cases=cases)
    line_items = [{"category": "도배", "description": "실크벽지", "amount": 1_150_000}]

    issues = await check_price_anomalies(_command(), line_items, engine)

    assert issues == []


@pytest.mark.asyncio
async def test_category_without_enough_cost_samples_is_skipped():
    # 사례는 3건 이상이라 전체 체크는 진행되지만, 창호 카테고리 cost 값이 있는 사례가
    # 부족하면 그 카테고리만 스킵되어야 한다 (다른 카테고리 부족과 섞이지 않게 검증).
    cases = [
        {"cost_도배": 1_000_000, "cost_창호": 2_000_000},
        {"cost_도배": 1_100_000},
        {"cost_도배": 1_200_000},
        {"cost_도배": 1_300_000},
    ]
    engine = _mock_engine(cases=cases)
    line_items = [
        {"category": "도배", "description": "실크벽지", "amount": 1_150_000},
        {"category": "창호", "description": "샷시", "amount": 9_000_000},
    ]

    issues = await check_price_anomalies(_command(), line_items, engine)

    assert issues == []


@pytest.mark.asyncio
async def test_p10_p90_range_wider_than_p25_p75_reduces_false_positives():
    # 실제 크롤링 견적서 4건으로 end-to-end 테스트한 결과, P25~P75(중간 50%)로는 카테고리
    # 하나당 벗어날 확률이 이미 ~50%라 8~12개 카테고리를 체크하면 거의 항상 뭔가는 걸렸다
    # (pipeline/results/risk_detector_migration.md 참고). 이 값(650,000)은 옛 P25~P75
    # 범위(700,000~1,200,000)로는 범위 밖이지만 P10~P90(600,000~1,300,000)으로는 정상이다.
    values = [500_000, 600_000, 700_000, 800_000, 900_000, 1_000_000, 1_100_000, 1_200_000, 1_300_000, 5_000_000]
    cases = [{"cost_도배": v} for v in values]
    engine = _mock_engine(cases=cases)
    line_items = [{"category": "도배", "description": "실크벽지", "amount": 650_000}]

    issues = await check_price_anomalies(_command(), line_items, engine)

    assert issues == []
