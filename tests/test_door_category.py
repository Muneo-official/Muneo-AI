"""
도어 공종 단위테스트 — 도어를 창호(샷시)·목공과 따로 집계하고, "창호"만 고른 요청은 설정에 따라 샷시 + 도어로 본다.

실제 사례: 실제 견적서와 비교했을 때 목공은 10건 중 9건이 과대였고, 창호는 도어만 한 견적에서 과대(+61 ~
+516%), 샷시를 한 견적에서 과소(−38%, −46%)였다. 코퍼스의 목공·창호 금액에 도어가 섞여 있었다. 금액은 지어낸 값이다.
"""

from app.domain.estimate_engine import EstimateEngine
from app.schemas.estimate import EstimateRequest


def _engine(**kwargs) -> EstimateEngine:
    return EstimateEngine(case_repository=None, embedder=None, reranker=None, **kwargs)


CASES = [
    {"total_cost": 40_000_000, "cost_창호": 8_000_000, "cost_도어": 2_000_000, "cost_목공": 3_000_000},
    {"total_cost": 20_000_000, "cost_창호": 0, "cost_도어": 1_500_000, "cost_목공": 2_000_000},  # 도어만 한 사례
    {"total_cost": 35_000_000, "cost_창호": 7_000_000, "cost_도어": 0, "cost_목공": 2_500_000},  # 샷시만 한 사례
]


def test_도어_공종은_도어_금액만_읽는다():
    _, costs = _engine().extract_costs(CASES, ["도어"])
    assert costs["도어"] == [2_000_000, 1_500_000]


def test_도어를_따로_고르면_창호는_샷시만이다():
    _, costs = _engine().extract_costs(CASES, ["창호", "도어"])
    assert costs["창호"] == [8_000_000, 7_000_000]
    assert costs["도어"] == [2_000_000, 1_500_000]


def test_창호만_고른_요청은_기본_설정에서_샷시와_도어의_합이다():
    # 화면에 "도어" 항목이 생기기 전에는 "창호"가 샷시와 도어를 함께 뜻한다
    _, costs = _engine().extract_costs(CASES, ["창호"])
    assert costs["창호"] == [10_000_000, 1_500_000, 7_000_000]


def test_설정을_끄면_창호만_고른_요청도_샷시만이다():
    _, costs = _engine(window_includes_door=False).extract_costs(CASES, ["창호"])
    assert costs["창호"] == [8_000_000, 7_000_000]


def test_도어를_분리하기_전의_사례는_창호_금액을_그대로_읽는다():
    # 재집계 전에는 cost_도어가 없고 cost_창호에 도어가 섞여 있다 — 지금까지와 같은 값이 나와야 한다
    old = [{"total_cost": 40_000_000, "cost_창호": 10_000_000}]
    _, costs = _engine().extract_costs(old, ["창호"])
    assert costs["창호"] == [10_000_000]


def test_도어는_창호와_한_공종으로_세어_전체_부분_판정이_달라지지_않는다():
    base = {"cost_도배": 1, "cost_바닥": 1, "cost_욕실": 1, "cost_가구": 1, "cost_전기": 1}
    # 도배·바닥·욕실·가구·전기에 창호와 도어 — 따로 세면 7개, 묶어 세면 6개. 어느 쪽이든 전체 리모델링이다
    assert EstimateEngine._is_partial_case({**base, "cost_창호": 1, "cost_도어": 1}) is False
    # 도배·바닥·욕실·가구·전기에 도어만 — 창호 공종 하나로 세어 6개
    assert EstimateEngine._is_partial_case({**base, "cost_도어": 1}) is False
    # 도어 없이 5개면 부분 시공 사례
    assert EstimateEngine._is_partial_case(base) is True


def test_요청_스키마가_도어_공종을_받는다():
    request = EstimateRequest(공종=["창호", "도어", "목공"], 평수=30)
    assert "도어" in request.공종
