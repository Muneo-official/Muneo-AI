"""
단가 기준표와 단가 판정(리스크 진단의 가격 이상 탐지) 단위테스트.

실제 사례: 공종의 합계를 비슷한 집의 합계와 비교하던 예전 방식은 실제 견적서 100건 중 74건에 가격 지적을 달았다.
집마다 공사 범위가 달라 합계는 원래 2배씩 차이 난다. 품목의 단가는 집이 달라도 비슷해서(도배 인건비 하루 25~30만 원),
단가를 비교하면 같은 표본에서 18건으로 줄고, 단가를 1.4배로 올린 견적서도 잡는다. 금액은 지어낸 값이다.
"""

from app.domain.unit_price_reference import (
    MIN_REQUESTS,
    UnitPriceReference,
    build_quantity_reference,
    build_reference,
    item_key,
    usable_unit_price,
)


def _item(category: str, description: str, unit_price: int, quantity: int = 2, unit: str = "식") -> dict:
    return {"category": category, "description": description, "unit_price": unit_price, "amount": unit_price * quantity, "unit": unit}


def _case(i: int, items: list[dict], **more) -> dict:
    return {"article_id": str(i), "request_url": f"https://example.com/{i}", "parsed_estimate": {"line_items": items}, **more}


def _cases(n: int = 20) -> list[dict]:
    # 의뢰마다 도배 인건비 25~30만 원, 실크벽지 평당 1.0~1.4만 원
    return [_case(i, [_item("도배", "인건비", 250_000 + (i % 6) * 10_000), _item("도배", "실크벽지(LX 베스띠)", 10_000 + (i % 5) * 1_000),
                      _item("도배", "부자재(풀, 부직포)", 200_000 + (i % 4) * 50_000)]) for i in range(n)]


# ── 열쇠와 단가 ────────────────────────────────────────────────────────────


def test_품명의_괄호_숫자_기호는_열쇠에서_뺀다():
    a = item_key({"category": "도배", "description": "실크벽지(LX 베스띠, 개나리 로하스)"})
    b = item_key({"category": "도배", "description": "실크 벽지 (LG,베스티) 2.5"})
    assert a == b == ("도배", "실크벽지", "")


def test_설비와_욕실은_같은_묶음이다():
    # 수전공사의 부속은 견적서에 따라 욕실로도 설비로도 읽힌다
    assert item_key({"category": "설비", "description": "샤워 수전"}) == item_key({"category": "욕실", "description": "샤워 수전"})


def test_단가가_없거나_금액보다_큰_줄은_비교하지_않는다():
    assert usable_unit_price({"unit_price": 300_000, "amount": 600_000}) == 300_000
    assert usable_unit_price({"unit_price": 0, "amount": 600_000}) is None
    assert usable_unit_price({"unit_price": 900_000, "amount": 600_000}) is None  # 열이 뒤바뀐 줄


# ── 기준표 ────────────────────────────────────────────────────────────────


def test_서로_다른_의뢰가_충분한_품목만_기준이_된다():
    cases = _cases(MIN_REQUESTS) + [_case(100 + i, [_item("가구", "한샘 싱크대", 3_000_000, 1)]) for i in range(MIN_REQUESTS - 1)]
    table = build_reference(cases)
    assert ("도배", "인건비", "식") in table and ("가구", "한샘싱크대", "식") not in table


def test_한_의뢰의_견적서_여러_장은_한_의뢰로_센다():
    # 수정본이 여러 장 달린 의뢰 하나가 기준을 만들지 못한다
    cases = [{**_case(i, [_item("목공", "특이한 품목", 100_000)]), "request_url": "https://example.com/same"} for i in range(40)]
    assert build_reference(cases) == {}


def test_비주거_견적서와_채점하는_의뢰는_기준에서_뺀다():
    cases = _cases(MIN_REQUESTS)
    assert build_reference(cases[:-1] + [{**cases[-1], "is_non_residential": True}]) == {}
    assert build_reference(cases, exclude_request="https://example.com/0") == {}
    assert ("도배", "인건비", "식") in build_reference(cases)


def test_기준은_단가의_하위_10퍼센트_중간_상위_10퍼센트다():
    stats = build_reference(_cases(20))[("도배", "인건비", "식")]
    assert (stats["n"], stats["p10"], stats["median"], stats["p90"]) == (20, 250_000, 270_000, 300_000)


# ── 판정 ──────────────────────────────────────────────────────────────────


def _reference() -> UnitPriceReference:
    return UnitPriceReference(build_reference(_cases(20)))


def test_범위를_넘고_중간값의_배수도_넘어야_높음이다():
    ref = _reference()
    assert ref.judge(_item("도배", "인건비", 560_000))[0] == "높음"
    assert ref.judge(_item("도배", "인건비", 310_000))[0] is None  # 상위 10%는 넘었지만 중간값의 1.3배 안
    assert ref.judge(_item("도배", "인건비", 120_000))[0] == "낮음"
    assert ref.judge(_item("도배", "처음 보는 품목", 560_000)) is None


def test_한_공종에서_비교한_줄의_절반_이상이_높으면_그_공종을_지적한다():
    items = [_item("도배", "인건비", 560_000), _item("도배", "실크벽지(LX)", 24_000), _item("도배", "부자재(풀)", 250_000)]
    issues = _reference().issues(items)
    assert [(i.process, i.title) for i in issues] == [("도배", "도배 단가가 시세보다 높음")]
    assert "3개 품목 중 2개" in issues[0].detail and "560,000원" in issues[0].detail


def test_한_줄만_벗어나면_지적하지_않는다():
    # 자재 한 줄이 비싼 것은 등급 차이일 수 있다. 공종 전체가 한쪽으로 쏠릴 때만 말한다
    items = [_item("도배", "인건비", 280_000), _item("도배", "실크벽지(수입)", 24_000), _item("도배", "부자재(풀)", 250_000)]
    assert _reference().issues(items) == []


def test_비교할_줄이_하나뿐인_공종은_지적하지_않는다():
    assert _reference().issues([_item("도배", "인건비", 900_000)]) == []


def test_단가가_낮은_쪽도_지적한다():
    items = [_item("도배", "인건비", 110_000), _item("도배", "실크벽지", 4_000)]
    assert [i.title for i in _reference().issues(items)] == ["도배 단가가 시세보다 낮음"]


# ── 수량 ──────────────────────────────────────────────────────────────────


def _measured(category: str, description: str, unit_price: int, quantity: float, unit: str) -> dict:
    return {"category": category, "description": description, "unit_price": unit_price, "amount": round(unit_price * quantity), "unit": unit}


def _quantity_cases(n: int = 20) -> list[dict]:
    # 30평 집에 실크벽지 85~104평(평당 2.8~3.5), 인건비 5 M/D
    return [{**_case(i, [_measured("도배", "실크벽지(LX)", 11_000, 85 + i, "평"), _measured("도배", "인건비", 280_000, 5, "M/D")]),
             "size_pyeong": 30} for i in range(n)]


def test_평당_수량_기준은_집_크기에_비례하는_단위의_품목만_만든다():
    table = build_quantity_reference(_quantity_cases())
    assert list(table) == [("도배", "실크벽지", "평")]  # M/D는 집 크기와 따로 논다
    assert table[("도배", "실크벽지", "평")]["median"] == 3.15


def test_단위_표기가_달라도_같은_단위로_본다():
    cases = [{**_case(i, [_measured("바닥", "강마루", 35_000, 80 + i, "㎡" if i % 2 else "M2")]), "size_pyeong": 30} for i in range(20)]
    assert list(build_quantity_reference(cases)) == [("바닥", "강마루", "m2")]


def test_평수에_비해_수량이_지나치게_많으면_지적한다():
    ref = UnitPriceReference({}, build_quantity_reference(_quantity_cases()))
    padded = _measured("도배", "실크벽지(LX 베스띠)", 11_000, 250, "평")  # 30평 집에 250평
    issues = ref.quantity_issues([padded], pyeong=30)
    assert [(i.process, i.title) for i in issues] == [("도배", "수량이 평수에 비해 많음")]
    assert "250평" in issues[0].detail


def test_수량이_많은_정상_견적서와_단위가_다른_줄은_지적하지_않는다():
    ref = UnitPriceReference({}, build_quantity_reference(_quantity_cases()))
    assert ref.quantity_issues([_measured("도배", "실크벽지", 11_000, 120, "평")], pyeong=30) == []    # 상위 10%의 1.5배 안
    assert ref.quantity_issues([_measured("도배", "실크벽지", 3_400, 400, "m2")], pyeong=30) == []     # ㎡ 기준이 없다
    assert ref.quantity_issues([_measured("도배", "실크벽지", 11_000, 250, "평")], pyeong=0) == []


# ── 코드 리뷰에서 나온 경우들 ──────────────────────────────────────────────


def test_금액이_숫자가_아닌_줄은_건너뛴다():
    # 모델이 "<UNKNOWN>"이나 "125,000" 같은 값을 낼 때가 있다. 한 줄 때문에 진단 전체가 실패하면 안 된다
    assert usable_unit_price({"unit_price": "<UNKNOWN>", "amount": 600_000}) is None
    assert usable_unit_price({"unit_price": "125,000", "amount": "3,000,000"}) == 125_000
    bad = {"category": "도배", "description": "인건비", "unit_price": "<UNKNOWN>", "amount": None}
    assert _reference().issues([bad, _item("도배", "인건비", 560_000), _item("도배", "실크벽지", 24_000)])


def test_지적의_공종_이름은_넘겨받은_함수로_정한다():
    # 분석기는 전기 품목을 "전기/조명"으로 부른다. 단가 지적이 다른 이름으로 나가면 품목과 다른 자리에 뜬다
    cases = [_case(i, [_item("전기", "인건비", 250_000 + (i % 6) * 10_000), _item("전기", "배선공사", 400_000)]) for i in range(20)]
    ref = UnitPriceReference(build_reference(cases))
    items = [_item("전기", "인건비", 700_000), _item("전기", "배선공사", 1_200_000)]
    assert [i.process for i in ref.issues(items)] == ["전기"]
    assert [i.process for i in ref.issues(items, process_of=lambda item: "전기/조명")] == ["전기/조명"]
    assert [i.title for i in ref.issues(items, process_of=lambda item: "전기/조명")] == ["전기/조명 단가가 시세보다 높음"]


# ── 단위 ──────────────────────────────────────────────────────────────────


def _mixed_unit_cases(n: int = 40) -> list[dict]:
    # 도배 부자재: 절반은 "식"으로 25~35만 원, 절반은 "㎡"로 1,500~1,700원 (실제 코퍼스의 모습)
    return [_case(i, [_item("도배", "부자재(풀, 부직포)", 200_000 + (i % 4) * 50_000, 1, "식") if i % 2
                      else _item("도배", "부자재(풀, 부직포)", 1_500 + (i % 4) * 100, 100, "㎡")]) for i in range(n)]


def test_같은_품목도_단위가_다르면_기준을_따로_만든다():
    # 한데 묶으면 범위가 1,500~350,000원이 되어, ㎡당 5,000원(시세의 3배)이 정상으로 지나간다
    table = build_reference(_mixed_unit_cases())
    assert table[("도배", "부자재", "m2")]["p90"] == 1_700 and table[("도배", "부자재", "식")]["p10"] == 250_000
    ref = UnitPriceReference(table)
    assert ref.judge(_item("도배", "부자재", 5_000, 100, "m2"))[0] == "높음"
    assert ref.judge(_item("도배", "부자재", 1_500, 100, "㎡"))[0] is None   # 묶었을 때는 "낮음"으로 걸렸다
    assert ref.judge(_item("도배", "부자재", 250_000, 1, "식"))[0] is None


def test_기준에_없는_단위의_줄은_비교하지_않는다():
    ref = UnitPriceReference(build_reference(_mixed_unit_cases()))
    assert ref.judge(_item("도배", "부자재", 4_000, 30, "평")) is None


def test_단위가_없는_줄은_품목의_단위가_하나로_모일_때만_비교한다():
    # 인건비는 모두 "식"이라 단위 칸이 없는 견적서의 인건비도 견줄 수 있다. 부자재는 식과 ㎡가 반반이라 견주지 않는다
    assert _reference().judge(_item("도배", "인건비", 560_000, unit=""))[0] == "높음"
    assert UnitPriceReference(build_reference(_mixed_unit_cases())).judge(_item("도배", "부자재", 5_000, 100, "")) is None


def test_단위가_안_적힌_코퍼스의_줄은_기준에_넣지_않는다():
    cases = [_case(i, [_item("도배", "인건비", 280_000, unit="")]) for i in range(20)]
    assert build_reference(cases) == {}


def test_사람_품을_세는_단위는_하나로_본다():
    cases = [_case(i, [_item("도배", "인건비", 280_000, 5, ("M/D", "인", "명")[i % 3])]) for i in range(20)]
    assert ("도배", "인건비", "인") in build_reference(cases)


def test_구분_이름이_그대로_남은_공종은_같은_공종으로_모은다():
    # 코퍼스에는 "도배공사", "조명공사", "목공사"처럼 견적서의 구분 이름이 그대로 남은 줄이 있다
    assert item_key({"category": "도배공사", "description": "인건비"}) == item_key({"category": "도배", "description": "인건비"})
    assert item_key({"category": "목공사", "description": "인건비"})[0] == "목공"
    assert item_key({"category": "조명공사", "description": "매입등"})[0] == "전기"
    assert item_key({"category": "수전공사", "description": "샤워 수전"}) == item_key({"category": "욕실", "description": "샤워 수전"})


def test_범위가_너무_넓은_품목은_기준으로_쓰지_않는다():
    # "창호 부자재 1식"은 8천 원부터 20만 원까지 있다. 이런 품목으로는 비싸다 싸다를 말할 수 없다
    cases = [_case(i, [_item("창호", "부자재", (8_000, 75_000, 200_000)[i % 3], 1)]) for i in range(30)]
    assert build_reference(cases) == {}
