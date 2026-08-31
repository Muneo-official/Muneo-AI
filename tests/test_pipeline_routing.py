from pipeline.routing import CONFIDENCE_THRESHOLD, route_case


def test_confidence_above_threshold_routes_to_estimate_cases():
    assert route_case(0.71) == "estimate_cases"


def test_confidence_below_threshold_routes_to_review_queue():
    assert route_case(0.69) == "review_queue"


def test_confidence_exactly_at_threshold_routes_to_estimate_cases():
    # 경계값은 통과시킨다 — validators.py의 confidence<0.7 집계 기준(review_queue)과
    # 정확히 반대 경계를 의도적으로 선택: "확실히 기준 미달"인 것만 review_queue로 보낸다
    assert route_case(CONFIDENCE_THRESHOLD) == "estimate_cases"


def test_confidence_1_0_routes_to_estimate_cases():
    assert route_case(1.0) == "estimate_cases"


def test_confidence_0_routes_to_review_queue():
    assert route_case(0.0) == "review_queue"
