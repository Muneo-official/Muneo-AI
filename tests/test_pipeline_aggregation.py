from pipeline.aggregation import build_category_costs, build_check_text, build_has_flags


def _item(category: str, description: str, amount: int) -> dict:
    return {"category": category, "description": description, "amount": amount}


def test_build_category_costs_sums_by_normalized_bucket():
    items = [
        _item("도배공사", "실크벽지", 500_000),
        _item("도배", "부자재", 100_000),
        _item("창호공사", "샷시", 1_000_000),
    ]
    costs = build_category_costs(items, total_cost=1_600_000)
    assert costs == {"cost_도배": 600_000, "cost_창호": 1_000_000}


def test_build_category_costs_ignores_unknown_category():
    items = [_item("냉난방공사", "에어컨", 300_000)]
    assert build_category_costs(items, total_cost=300_000) == {}


def test_build_category_costs_rescales_when_line_sum_exceeds_total():
    # 대분류/소분류 중복 집계로 line_items 합계가 total_cost의 1.3배를 넘으면 비율 재산정
    items = [_item("도배공사", "a", 1_000_000), _item("타일공사", "b", 1_000_000)]
    costs = build_category_costs(items, total_cost=1_000_000)
    assert costs["cost_도배"] + costs["cost_타일"] == 1_000_000


def test_build_check_text_combines_request_text_and_line_items():
    text = build_check_text("욕실 리모델링 요청", [_item("타일공사", "욕실타일", 100_000)])
    assert "욕실" in text
    assert "타일공사" in text
    assert "욕실타일" in text


def test_build_has_flags_detects_keyword_in_request_text():
    flags = build_has_flags("창호 교체 부탁드립니다", [])
    assert flags["has_창호"] == "true"
    assert flags["has_욕실"] == "false"


def test_build_has_flags_detects_keyword_in_line_items():
    flags = build_has_flags("", [_item("도배공사", "실크벽지", 100_000)])
    assert flags["has_도배"] == "true"


def test_build_has_flags_returns_all_known_keys():
    flags = build_has_flags("", [])
    assert set(flags.keys()) == {
        "has_창호", "has_도배", "has_타일", "has_가구",
        "has_욕실", "has_바닥", "has_전기", "has_조명",
    }
    assert all(v == "false" for v in flags.values())
