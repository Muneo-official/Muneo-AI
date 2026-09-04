"""
EstimateEngine.calc_공종_factors() 중 가구/주방 분배 로직 단위테스트.

Atlas의 estimate_cases를 라인아이템 단위로 까본 결과, cost_가구 필드가 있는 case의
71%(46/65)는 주방(싱크대 등) 항목 없이 순수 가구 비용만 담고 있었다. 그런데 기존 코드는
"가구"만 선택해도 무조건 40%로 깎았는데, 이는 대부분의 경우 이미 맞는 값을 근거 없이
반토막 내는 것이었다. 이제 가구 단독 선택 시에는 보정을 적용하지 않는다.

주방+가구 동시 선택 시의 60/40 분배는 그대로 유지한다 — case 안에 실제로 주방+가구가
함께 잡히는 표본이 3건뿐이라(그마저도 가구 쪽이 더 컸음) 재조정할 근거가 부족하다.
"""

from app.domain.estimate_engine import EstimateEngine


def _engine() -> EstimateEngine:
    return EstimateEngine(case_repository=None, embedder=None, reranker=None)


def test_가구_단독_선택_시_보정_없음():
    factors = _engine().calc_공종_factors({"공종": ["가구"], "방개수": 3})

    assert "가구" not in factors


def test_주방_가구_동시_선택_시_60_40_분배_유지():
    factors = _engine().calc_공종_factors({"공종": ["주방", "가구"], "방개수": 3})

    assert factors["주방"][0] == 0.60
    assert factors["가구"][0] == 0.40


def test_주방_단독_선택_시_보정_없음():
    factors = _engine().calc_공종_factors({"공종": ["주방"], "방개수": 3})

    assert "주방" not in factors
