"""
EstimateEngine.extract_costs() 단위테스트 — 욕실처럼 cost_* 키가 여러 개인 공종은 사례별 합으로 집계한다.

실제 사례: 실제 업체 견적서 5건과 비교했을 때 욕실만 5건 모두 시스템 범위 최대치의 2.0~2.5배로
과소평가됐다. cost_욕실·cost_설비·cost_타일을 사례별로 더하지 않고 각각 따로 리스트에 넣어
중앙값을 냈기 때문에, 욕실 금액이 세 부분의 합이 아니라 부분 하나의 크기로 나왔다.
(코퍼스 709건 기준 — 섞은 중앙값 2,790,000원 vs 사례별 합의 중앙값 7,855,000원)
"""

from app.domain.estimate_engine import EstimateEngine


def _engine() -> EstimateEngine:
    return EstimateEngine(case_repository=None, embedder=None, reranker=None)


def test_욕실은_사례별로_욕실_설비_타일을_합산한다():
    cases = [
        {"total_cost": 40_000_000, "cost_욕실": 2_000_000, "cost_설비": 3_000_000, "cost_타일": 4_000_000},
        {"total_cost": 35_000_000, "cost_욕실": 1_500_000, "cost_설비": 2_500_000, "cost_타일": 3_500_000},
    ]

    _, cat_costs = _engine().extract_costs(cases, ["욕실"])

    assert cat_costs["욕실"] == [9_000_000, 7_500_000]


def test_욕실_키가_일부만_있는_사례는_있는_값만_합산한다():
    # 같은 욕실 공사를 욕실 없이 설비·타일로만 분류한 사례가 코퍼스에 127건 있다.
    cases = [
        {"total_cost": 30_000_000, "cost_설비": 2_500_000, "cost_타일": 3_500_000},
        {"total_cost": 20_000_000, "cost_타일": 1_000_000},
    ]

    _, cat_costs = _engine().extract_costs(cases, ["욕실"])

    assert cat_costs["욕실"] == [6_000_000, 1_000_000]


def test_욕실_관련_금액이_전혀_없는_사례는_욕실_집계에서_빠진다():
    cases = [
        {"total_cost": 10_000_000, "cost_도배": 3_000_000},
        {"total_cost": 12_000_000, "cost_욕실": 0, "cost_설비": None, "cost_타일": 0},
    ]

    total_costs, cat_costs = _engine().extract_costs(cases, ["욕실"])

    assert "욕실" not in cat_costs
    assert total_costs == [10_000_000, 12_000_000]


def test_키가_하나인_공종은_기존과_동일하게_집계한다():
    cases = [
        {"total_cost": 40_000_000, "cost_도배": 3_000_000, "cost_창호": 8_000_000},
        {"total_cost": 35_000_000, "cost_도배": 2_800_000},
    ]

    _, cat_costs = _engine().extract_costs(cases, ["도배", "창호"])

    assert cat_costs["도배"] == [3_000_000, 2_800_000]
    assert cat_costs["창호"] == [8_000_000]
