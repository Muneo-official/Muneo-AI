"""
RiskAnalyzer(리스크 진단의 규칙 판정) 단위테스트.

실제 사례: 실제 견적서 368건에 예전 규칙을 적용하면 견적서 한 장에 지적이 보통 6개 나왔고, 98%가 "누락" 지적을
받았다. 모호 표현 지적 842건 중 827건이 "기타"라는 낱말 하나 때문이었고, 필수 항목 표에는 하지 않아도 되는 공사
(천장/가벽, 문틀/도어, 배관)가 들어 있었다. 금액은 지어낸 값이다.
"""

from app.domain.risk_analyzer import RiskAnalyzer


def _item(category: str, description: str, amount: int = 100_000, quantity: float = 1, **more) -> dict:
    return {"category": category, "description": description, "amount": amount, "unit_price": amount, "quantity": quantity, **more}


def _issues(items: list[dict], kind: str | None = None) -> list:
    issues, _ = RiskAnalyzer().analyze(items)
    return [i for i in issues if kind is None or i.type == kind]


# ── 누락 ──────────────────────────────────────────────────────────────────


def test_철거에_폐기물_처리가_없으면_누락이다():
    issues, processes = RiskAnalyzer().analyze([_item("철거", "바닥 철거")])
    assert processes == ["철거"]
    assert [(i.type, i.process) for i in issues] == [("누락", "철거")]


def test_폐기물_처리는_다른_공종에_적혀_있어도_된다():
    assert _issues([_item("철거", "바닥 철거"), _item("공과잡비", "공사 폐기물처리")], "누락") == []


def test_욕실_타일을_하는데_방수가_없으면_누락이다():
    issues = _issues([_item("타일", "화장실 벽타일/300*600"), _item("타일", "인건비")], "누락")
    assert [i.process for i in issues] == ["욕실"] and "방수" in issues[0].detail


def test_방수는_기타공사에_적혀_있어도_된다():
    assert _issues([_item("타일", "욕실 바닥타일"), _item("공과잡비", "화장실 방수공사")], "누락") == []


def test_욕실_타일이_없으면_방수를_요구하지_않는다():
    # 수전만 바꾸거나 주방 타일만 하는 공사
    assert _issues([_item("설비", "세면기 수전"), _item("타일", "주방벽타일")], "누락") == []


def test_하지_않아도_되는_공사는_누락으로_지적하지_않는다():
    items = [_item("목공", "걸레받이"), _item("전기", "매입등 배선"), _item("설비", "샤워 수전"), _item("가구", "싱크대")]
    assert _issues(items, "누락") == []


def test_소계_행만_있으면_실제_품목이_없는_것이다():
    # "폐기물 소계" 같은 집계 행의 낱말로 필수 항목이 있다고 보면 안 된다
    assert len(_issues([_item("철거", "바닥 철거"), _item("철거", "폐기물 소계")], "누락")) == 1


# ── 중복 ──────────────────────────────────────────────────────────────────


def test_같은_공종에_품명과_금액이_같은_줄이_둘이면_중복이다():
    issues = _issues([_item("도배", "실크벽지 시공", 500_000), _item("도배", "실크벽지 시공", 500_000)], "중복")
    assert [i.title for i in issues] == ["동일 항목 중복 기재"]


def test_인건비처럼_공종마다_따로_드는_줄은_중복이_아니다():
    items = [_item("욕실", "인건비", 300_000), _item("욕실", "인건비", 300_000), _item("욕실", "식대", 15_000), _item("욕실", "식대", 15_000)]
    assert _issues(items, "중복") == []


def test_다른_공종에_같은_줄이_있으면_중복이다():
    issues = _issues([_item("철거", "욕실 벽타일 철거", 800_000), _item("공과잡비", "욕실 벽타일 철거", 800_000)], "중복")
    assert [i.title for i in issues] == ["다른 공종에 같은 항목"] and "철거·공과잡비" in issues[0].detail


def test_나누는_방식이_흔들리는_공종_사이의_같은_줄은_중복이_아니다():
    # 같은 수전이 욕실로도 설비로도 읽힌다 — 견적서의 중복이 아니라 분류의 흔들림이다
    assert _issues([_item("욕실", "샤워 수전", 95_000), _item("설비", "샤워 수전", 95_000)], "중복") == []


# ── 불분명 ────────────────────────────────────────────────────────────────


def test_별도_협의_같은_낱말은_모호한_표현이다():
    issues = _issues([_item("목공", "몰딩 별도 협의", 200_000)], "불분명")
    assert [i.title for i in issues] == ["모호한 표현 포함"]


def test_기타는_모호한_표현이_아니다():
    assert _issues([_item("타일", "부자재(백시멘트, 실리콘, 기타)"), _item("전기", "기타 자재(전선외)")], "불분명") == []


def test_금액이_없는_줄은_지적하고_단가만_없는_줄은_지적하지_않는다():
    no_amount = {"category": "타일", "description": "욕실 타일", "amount": 0, "unit_price": None}
    no_unit_price = {"category": "타일", "description": "주방 타일 1식", "amount": 1_200_000, "unit_price": 0}
    issues = _issues([no_amount, no_unit_price, _item("공과잡비", "방수")], "불분명")
    assert [i.title for i in issues] == ["총액에 포함되지 않은 항목"] and "욕실 타일" in issues[0].detail


def test_금액_없이_별도라고_적힌_줄은_지적을_하나만_한다():
    # 실제 사례: "별도, 현장 협의"로 적힌 줄은 총액에 들어가지 않은 비용이다. 모호한 표현 지적까지 겹쳐 내지 않는다
    item = {"category": "철거", "description": "욕실 벽타일철거 (별도, 현장 협의)", "amount": 0, "unit_price": 0}
    assert [i.title for i in _issues([item, _item("철거", "폐기물 처리")], "불분명")] == ["총액에 포함되지 않은 항목"]


def test_큰_금액이_수량_없는_한_줄뿐이면_일괄_금액으로_지적한다():
    issues = _issues([_item("필름", "방문 틀 창호 붙박이", 4_800_000)], "불분명")
    assert [i.title for i in issues] == ["세부 내역 없이 일괄 금액"]


def test_수량과_단가가_적힌_한_줄이나_작은_금액은_일괄_금액이_아니다():
    assert _issues([_item("바닥", "강마루", 3_000_000, quantity=24)], "불분명") == []
    assert _issues([_item("도장", "발코니 탄성코트", 500_000)], "불분명") == []


# ── 공종 ──────────────────────────────────────────────────────────────────


def test_창호_필름_기타공사는_품명이_아니라_카테고리로_공종을_정한다():
    # 예전에는 이 카테고리를 몰라서 창호공사의 "ABS도어+문틀"이 목공으로 들어갔다
    _, processes = RiskAnalyzer().analyze([_item("창호", "ABS도어+문틀"), _item("필름", "문틀 필름"), _item("공과잡비", "주민동의서 대행")])
    assert processes == ["공과잡비", "창호", "필름"]


def test_모르는_카테고리와_품명은_분석에서_빠진다():
    issues, processes = RiskAnalyzer().analyze([_item("에어컨공사", "냉난방기 설치")])
    assert (issues, processes) == ([], [])


# ── 소계 검산 ──────────────────────────────────────────────────────────────


def _subtotal(category: str, amount: int) -> dict:
    return {"category": category, "description": "소계", "amount": amount, "unit_price": 0}


def test_소계가_품목_합과_다르면_지적한다():
    items = [_subtotal("도배", 4_724_000), _item("도배", "실크벽지", 2_295_000), _item("도배", "인건비", 2_000_000),
             _subtotal("바닥", 3_000_000), _item("바닥", "강마루", 3_000_000, quantity=24)]
    issues = [i for i in _issues(items) if i.title == "소계가 품목 합과 다름"]
    assert [i.process for i in issues] == ["도배"]
    assert "4,724,000원" in issues[0].detail and "4,295,000원" in issues[0].detail


def test_소계가_품목들_뒤에_오는_양식도_검산한다():
    items = [_item("도배", "실크벽지", 2_295_000), _item("도배", "인건비", 2_000_000), _subtotal("도배", 4_295_000),
             _item("바닥", "강마루", 3_000_000, quantity=24), _subtotal("바닥", 3_300_000)]
    assert [i.process for i in _issues(items) if i.title == "소계가 품목 합과 다름"] == ["바닥"]


def test_소계_행은_품목으로_세지_않는다():
    # 소계 행이 필수 항목·중복·일괄 금액 판정에 섞이면 안 된다
    items = [_subtotal("필름", 4_800_000), _item("필름", "문틀 필름", 2_400_000, quantity=5), _item("필름", "문짝 필름", 2_400_000, quantity=5)]
    assert _issues(items) == []


def test_소계_행이_없는_견적서는_검산하지_않는다():
    assert [i for i in _issues([_item("도배", "실크벽지", 2_295_000)]) if "소계" in i.title] == []
