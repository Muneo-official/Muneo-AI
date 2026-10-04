import pytest

from pipeline.aggregation import (
    build_category_costs,
    build_check_text,
    build_has_flags,
    category_amounts,
    is_door_item,
)


def _item(category: str, description: str, amount: int) -> dict:
    return {"category": category, "description": description, "amount": amount}


def test_build_category_costs_sums_by_normalized_bucket():
    items = [
        _item("도배공사", "실크벽지", 500_000),
        _item("도배", "부자재", 100_000),
        _item("창호공사", "샷시", 1_000_000),
    ]
    costs = build_category_costs(items, total_cost=1_600_000)
    # cost_도어는 도어가 없어도 0으로 들어간다 — 도어를 분리한 기준으로 집계된 사례라는 표시다
    assert costs == {"cost_도배": 600_000, "cost_창호": 1_000_000, "cost_도어": 0}


def test_build_category_costs_ignores_unknown_category():
    items = [_item("냉난방공사", "에어컨", 300_000)]
    assert build_category_costs(items, total_cost=300_000) == {"cost_도어": 0}


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


# ── 도어 분리 ─────────────────────────────────────────────────────────────
# 견적서 양식에 따라 도어가 창호공사에도, 목공에도 들어간다. 그대로 집계하면 목공에는 도어가 든 것과 안 든
# 것이, 창호에는 샷시만·도어만·둘 다인 것이 섞인다. 금액은 지어낸 값이다.


@pytest.mark.parametrize("description", [
    "ABS도어(민판) 900×2100", "현관중문 3연동(엣지)", "방, 욕실 문틀", "9mm 문선조성(고밀도MDF)",
    "목문 손잡이(일반)", "디지털도어록(푸시풀)", "원터치 도어 스토퍼", "단열현관문(1000*2100)", "방화문 도어록",
    "세탁실 접이문", "현관 슬림형 줄문(비대칭,3안통)", "창고 문틀", "욕실문 교체", "ABS 욕실 다용도실 (900x2100)",
])
def test_is_door_item_문짝과_문틀_부속은_도어다(description):
    assert is_door_item(description) is True


@pytest.mark.parametrize("description", [
    "터닝 발코니도어", "폴딩도어", "LX터닝 발코니도어(D140)", "터닝도어 목공마감 시공비",  # 샷시 업체가 시공하는 창호 제품
    "KCC 발코니 이중창", "인건비", "몰딩, 걸레받이", "각재", "전문 인테리어공사",
    "붙박이장 도어", "신발장 문짝 교체", "싱크대 손잡이 교체",  # 가구 문짝은 가구다
    "ABS 몰딩", "방문 실측 및 상담", "outdoor 조명 박스", "욕실문지방 인조대리석", "방충문",
])
def test_is_door_item_발코니_쪽_문과_일반_품목은_도어가_아니다(description):
    assert is_door_item(description) is False


def test_목공에_든_도어는_도어로_옮기고_목공_인건비는_목공에_둔다():
    items = [
        _item("목공", "ABS도어(민판)", 1_500_000),
        _item("목공", "방, 욕실 문틀", 600_000),
        _item("목공", "인건비", 2_000_000),
        _item("목공", "각재", 300_000),
    ]
    assert category_amounts(items) == {"도어": 2_100_000, "목공": 2_300_000}


def test_창호에_도어만_있으면_인건비까지_전부_도어다():
    # 그대로 두면 도어만 한 견적에 "샷시 금액"이 인건비만큼 남는다
    items = [
        _item("창호", "현관중문 3연동", 1_100_000),
        _item("창호", "인건비", 300_000),
        _item("창호", "식대", 20_000),
    ]
    assert category_amounts(items) == {"도어": 1_420_000}


def test_창호에_샷시와_도어가_함께_있으면_일반_품목을_금액_비율로_나눈다():
    items = [
        _item("창호", "KCC 발코니 이중창", 6_000_000),
        _item("창호", "ABS도어", 2_000_000),
        _item("창호", "인건비", 800_000),  # 샷시 6 : 도어 2 → 60만 : 20만
    ]
    assert category_amounts(items) == {"창호": 6_600_000, "도어": 2_200_000}


def test_터닝도어는_창호에_남는다():
    items = [_item("창호", "터닝 발코니도어", 600_000), _item("창호", "방문 손잡이", 50_000)]
    assert category_amounts(items) == {"창호": 600_000, "도어": 50_000}


def test_철거_필름_가구로_분류된_문짝_품목은_건드리지_않는다():
    items = [
        _item("철거", "문짝 철거", 100_000),
        _item("필름", "방문 필름", 400_000),
        _item("가구", "붙박이장 도어", 900_000),
    ]
    assert category_amounts(items) == {"철거": 100_000, "필름": 400_000, "가구": 900_000}


def test_도어_분리는_금액을_옮기기만_한다():
    items = [
        _item("목공사", "ABS도어", 1_000_000), _item("목공사", "인건비", 2_000_000),
        _item("창호공사", "샷시", 5_000_000), _item("창호공사", "중문", 1_000_000), _item("창호공사", "운송비", 123_457),
        _item("도배공사", "실크벽지", 3_000_000),
    ]
    costs = build_category_costs(items, total_cost=12_123_457)
    assert sum(costs.values()) == pytest.approx(12_123_457, abs=3)
    assert costs["cost_목공"] == 2_000_000
    assert costs["cost_도어"] > 2_000_000


def test_category_amounts는_이미_정규화된_공종도_받는다():
    # 리스크 진단의 품목은 파서가 정규화한 값을 그대로 쓴다
    items = [_item("창호", "중문", 1_000_000), _item("냉난방", "에어컨", 500_000)]
    assert category_amounts(items, normalize=lambda c: c) == {"도어": 1_000_000, "냉난방": 500_000}


@pytest.mark.parametrize("description", ["시스템창 교체", "거실창 / 안방창 교체 (PVC)", "거실 픽스창", "전체 창호 교체 공사"])
def test_도어가_없는_창호는_일반_품목이_있어도_창호에_남는다(description):
    # 샷시로 인식하지 못한 품목을 도어로 넘기면, 샷시만 한 견적이 샷시 금액만 한 "도어"가 된다
    items = [_item("창호공사", description, 8_000_000), _item("창호공사", "인건비", 500_000)]
    assert category_amounts(items) == {"창호": 8_500_000}


def test_도어가_없으면_샷시로_인식하지_못한_품목도_창호에_둔다():
    items = [_item("창호", "하부 프레임 보강", 700_000), _item("창호", "인건비", 300_000)]
    assert category_amounts(items) == {"창호": 1_000_000}


def test_도어공사에_적힌_가구_문짝은_도어로_옮기지_않는다():
    # 정규화 표가 도어공사를 창호로 보내므로, 가구 문짝은 창호에 남는다(가구로 옮기는 것은 validators의 몫)
    costs = build_category_costs([_item("도어공사", "붙박이장 도어", 900_000)], total_cost=900_000)
    assert costs == {"cost_창호": 900_000, "cost_도어": 0}
