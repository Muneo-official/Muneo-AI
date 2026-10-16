from pipeline.parsing import merge_and_validate, merge_chunk_results, merge_parsed_results


def _result(total_cost: int, line_items: list[dict]) -> dict:
    return {"is_estimate": True, "total_cost": total_cost, "line_items": line_items}


def _item(category: str, description: str, amount: int) -> dict:
    return {"category": category, "description": description, "amount": amount}


def test_merge_single_image_removes_aggregate_rows():
    result = _result(1_000_000, [
        _item("도배공사", "실크벽지", 800_000),
        _item("도배공사", "합계", 800_000),  # 집계 행 — 제거돼야 함
    ])
    merged = merge_parsed_results([result])
    assert len(merged["line_items"]) == 1
    assert merged["line_items"][0]["description"] == "실크벽지"


def test_merge_picks_most_consistent_image_when_totals_differ():
    # 서로 다른 견적서가 섞인 상황 — 내부 일관성(line_sum ≈ total_cost)이 높은 쪽을 선택해야 함
    consistent = _result(1_000_000, [_item("도배공사", "실크벽지", 950_000)])
    inconsistent = _result(2_000_000, [_item("타일공사", "욕실타일", 500_000)])
    merged = merge_parsed_results([inconsistent, consistent])
    assert merged["total_cost"] == 1_000_000


def test_merge_no_estimate_results_returns_empty():
    assert merge_parsed_results([{"is_estimate": False}]) == {}


def test_merge_and_validate_attaches_validation_block():
    result = _result(1_000_000, [_item("도배공사", "실크벽지", 950_000)])
    merged = merge_and_validate([result], size_pyeong=33)
    assert "_validation" in merged
    assert merged["_validation"]["confidence"] == 1.0
    assert merged["_validation"]["issues"] == []


def test_merge_and_validate_flags_known_bug_pattern_immediately():
    """실제 발견 사례(article_id=890396)와 동일한 패턴 — "도어공사"로 분류된 가구 문짝
    항목이 파싱 직후 바로 재분류 제안으로 잡혀야 한다 (사후 소급 검증을 기다리지 않고)."""
    result = _result(1_000_000, [_item("도어공사", "안방 붙박이장 문짝교체", 1_000_000)])
    merged = merge_and_validate([result], size_pyeong=32)
    assert len(merged["_validation"]["reclassification_suggestions"]) == 1


def test_merge_and_validate_flags_size_pyeong_corruption_immediately():
    """실제 발견 패턴(docs/IMPLEMENTATION_LOG.md 2-4) — size_pyeong에 article_id 숫자가
    잘못 들어간 경우가 파싱 직후 바로 error로 잡혀야 한다."""
    result = _result(1_000_000, [_item("도배공사", "실크벽지", 950_000)])
    merged = merge_and_validate([result], size_pyeong=877930)
    assert merged["_validation"]["confidence"] < 1.0
    assert any(i["rule"] == "size_pyeong_range" for i in merged["_validation"]["issues"])


def test_merge_and_validate_empty_results_returns_empty():
    assert merge_and_validate([{"is_estimate": False}], size_pyeong=30) == {}


def test_merge_chunk_results_dedups_overlap_between_chunks():
    # 세로로 긴 이미지를 200px 겹침으로 나누면 같은 행이 두 청크에 걸쳐 두 번 나올 수
    # 있다 — (category, amount, unit_price) 조합으로 중복 제거돼야 한다.
    chunk1 = _result(1_000_000, [_item("도배공사", "실크벽지", 900_000)])
    chunk2 = _result(1_000_000, [_item("도배공사", "실크벽지", 900_000)])  # 겹침 구간 재파싱
    merged = merge_chunk_results([chunk1, chunk2])
    assert len(merged["line_items"]) == 1


def test_merge_chunk_results_uses_max_total_cost_across_chunks():
    # 합계 행은 보통 마지막 청크에만 보인다 — 다른 청크는 total_cost=0일 수 있음
    chunk1 = _result(0, [_item("도배공사", "실크벽지", 900_000)])
    chunk2 = _result(1_000_000, [_item("타일공사", "욕실타일", 100_000)])
    merged = merge_chunk_results([chunk1, chunk2])
    assert merged["total_cost"] == 1_000_000
    assert len(merged["line_items"]) == 2


def test_merge_chunk_results_no_estimate_returns_is_estimate_false():
    assert merge_chunk_results([{"is_estimate": False}]) == {"is_estimate": False}


def test_merge_chunk_results_tolerates_non_numeric_total_cost():
    # 실측: risk_detector 실제 이미지 배치 테스트에서 Vision API가 total_cost를
    # 정수 대신 "<UNKNOWN>" 문자열로 반환해 int() 캐스팅이 그대로 크래시했다.
    chunk = _result("<UNKNOWN>", [_item("도배공사", "실크벽지", 900_000)])
    merged = merge_chunk_results([chunk])
    assert merged["total_cost"] == 0
    assert len(merged["line_items"]) == 1


def test_merge_parsed_results_tolerates_non_numeric_total_cost():
    result = _result("<UNKNOWN>", [_item("도배공사", "실크벽지", 900_000)])
    merged = merge_parsed_results([result])
    assert merged["total_cost"] == 0


def test_merge_parsed_results_multi_image_tolerates_non_numeric_total_cost():
    ok = _result(1_000_000, [_item("도배공사", "실크벽지", 950_000)])
    unknown = _result("<UNKNOWN>", [_item("타일공사", "욕실타일", 500_000)])
    merged = merge_parsed_results([ok, unknown])
    assert merged["total_cost"] == 1_000_000


# ── 청크 병합: 겹친 구간의 중복, 리스크 진단용 행 ─────────────────────────


def _chunk(items: list[dict], total: int = 0) -> dict:
    return {"is_estimate": True, "total_cost": total, "line_items": items}


def test_겹친_구간의_같은_행이_청크마다_다른_공종으로_읽혀도_한_번만_남는다():
    # 실제 사례: 싱크볼이 한 청크에서는 가구, 다른 청크에서는 설비로 읽혀 두 공종에 하나씩 남았다
    from pipeline.parsing import merge_chunk_results

    row = {"code": "1305", "description": "사각 싱크볼", "amount": 390_000, "unit_price": 390_000}
    merged = merge_chunk_results([_chunk([{**row, "category": "가구"}]), _chunk([{**row, "category": "설비"}])])
    assert [i["category"] for i in merged["line_items"]] == ["가구"]


def test_수집에서는_금액_없는_행과_소계_행을_버리고_리스크_진단에서는_남긴다():
    from pipeline.parsing import is_subtotal_row, merge_chunk_results

    items = [
        {"code": "100", "category": "철거", "description": "소계", "amount": 1_000_000, "unit_price": 0},
        {"code": "101", "category": "철거", "description": "철거 인건비", "amount": 1_000_000, "unit_price": 250_000},
        {"code": "102", "category": "철거", "description": "욕실 벽타일철거 (별도, 현장 협의)", "amount": 0, "unit_price": 0},
        {"code": "", "category": "철거", "description": "합계", "amount": 1_000_000, "unit_price": 0},
    ]
    assert [i["description"] for i in merge_chunk_results([_chunk(items)])["line_items"]] == ["철거 인건비"]

    risk = merge_chunk_results([_chunk(items)], for_risk=True)["line_items"]
    assert [i["description"] for i in risk] == ["소계", "철거 인건비", "욕실 벽타일철거 (별도, 현장 협의)"]
    assert [is_subtotal_row(i) for i in risk] == [True, False, False]


def test_금액_없는_행이_겹친_구간에서_두_번_읽혀도_한_번만_남는다():
    from pipeline.parsing import merge_chunk_results

    row = {"code": "1516", "category": "공과잡비", "description": "승강기 이용료 별도", "amount": 0, "unit_price": 0}
    assert len(merge_chunk_results([_chunk([row]), _chunk([dict(row)])], for_risk=True)["line_items"]) == 1


def test_코드가_없는_행은_공종이_다르면_같은_금액이어도_합치지_않는다():
    # 목공의 "인건비 300,000"과 타일의 "인건비 300,000"은 다른 줄이다. 합치면 뒤쪽 공종의 금액이 사라진다
    from pipeline.parsing import merge_chunk_results

    items = [{"category": "목공", "description": "인건비", "amount": 300_000, "unit_price": 300_000},
             {"category": "타일", "description": "인건비", "amount": 300_000, "unit_price": 300_000}]
    assert [i["category"] for i in merge_chunk_results([_chunk(items)])["line_items"]] == ["목공", "타일"]


def _rows(merged: dict) -> list[str]:
    return [i["description"] for i in merged["line_items"]]


def test_같은_청크_안에서_앞_글자와_금액이_같은_두_줄은_둘_다_남는다():
    # 실제 사례: 전기 소계 860,000원 견적서에서 "LED 다운라이트 주방"이 "거실"과 한 줄로 합쳐져 빠졌고,
    # 리스크 진단이 "품목을 더하면 815,000원"이라고 맞는 소계를 지적했다
    from pipeline.parsing import merge_chunk_results

    items = [
        {"category": "전기", "description": "소계", "amount": 860_000, "unit_price": 0},
        {"category": "전기", "description": "LED 다운라이트 거실", "amount": 45_000, "unit_price": 15_000},
        {"category": "전기", "description": "LED 다운라이트 주방", "amount": 45_000, "unit_price": 15_000},
        {"category": "전기", "description": "콘센트·스위치 교체", "amount": 770_000, "unit_price": 770_000},
    ]
    merged = merge_chunk_results([_chunk(items)], for_risk=True)
    assert _rows(merged) == ["소계", "LED 다운라이트 거실", "LED 다운라이트 주방", "콘센트·스위치 교체"]


def test_겹친_구간에서_다시_읽힌_줄만_빠지고_앞뒤_줄은_남는다():
    from pipeline.parsing import merge_chunk_results

    a = {"category": "도배", "description": "실크벽지", "amount": 900_000, "unit_price": 9_000}
    b = {"category": "도배", "description": "도배 인건비", "amount": 500_000, "unit_price": 250_000}
    c = {"category": "타일", "description": "욕실 벽타일", "amount": 700_000, "unit_price": 35_000}
    assert _rows(merge_chunk_results([_chunk([a, b]), _chunk([dict(b), c])])) == ["실크벽지", "도배 인건비", "욕실 벽타일"]


def test_견적서에_두_번_적힌_줄이_겹친_구간에서_한_번_더_읽혀도_두_줄이_남는다():
    from pipeline.parsing import merge_chunk_results

    row = {"category": "전기", "description": "LED 매입등", "amount": 45_000, "unit_price": 15_000}
    merged = merge_chunk_results([_chunk([row, dict(row)]), _chunk([dict(row)])])
    assert len(merged["line_items"]) == 2


def test_이웃하지_않은_청크의_같은_열쇠_줄은_겹친_것이_아니다():
    # 겹침은 바로 앞 청크와만 생긴다. 첫 청크와 셋째 청크에 각각 있는 줄은 서로 다른 줄이다
    from pipeline.parsing import merge_chunk_results

    row = {"category": "전기", "description": "LED 매입등", "amount": 45_000, "unit_price": 15_000}
    other = {"category": "도배", "description": "실크벽지", "amount": 900_000, "unit_price": 9_000}
    merged = merge_chunk_results([_chunk([row]), _chunk([other]), _chunk([dict(row)])])
    assert _rows(merged) == ["LED 매입등", "실크벽지", "LED 매입등"]


def test_겹친_구간의_줄이_다음_청크에서_다른_공종과_품명으로_읽혀도_위치로_맞춰_뺀다():
    # 실제 사례(docs/samples/test_견적서.png): 다음 청크에서는 구분 이름이 잘려 가구 줄을 "확장"으로, "소계"를
    # "소 계 (확장포함)"로 읽었다. 공종이 달라 열쇠로는 안 걸러져 가구 345,000원이 바닥 소계 밑에 붙었다
    from pipeline.parsing import merge_chunk_results

    def row(category, description, amount, unit_price):
        return {"code": "", "category": category, "description": description, "amount": amount, "unit_price": unit_price}

    chunk1 = [row("가구", "소계", 3_805_000, 0), row("가구", "시공인건비", 300_000, 300_000),
              row("가구", "식대", 15_000, 15_000), row("가구", "운송비", 30_000, 30_000),
              row("바닥", "소계", 1_699_000, 0), row("바닥", "우드륨 2.2t", 1_334_000, 58_000)]
    chunk2 = [row("확장", "시공인건비", 300_000, 300_000), row("확장", "식대", 15_000, 15_000),
              row("확장", "운송비", 30_000, 30_000), row("바닥", "소 계 (확장포함)", 1_699_000, 0),
              row("바닥", "우드륨 2.2t", 1_334_000, 58_000), row("바닥", "인건비", 280_000, 280_000),
              row("바닥", "식대", 15_000, 15_000)]
    merged = merge_chunk_results([_chunk(chunk1), _chunk(chunk2)], for_risk=True)
    assert [(i["category"], i["description"]) for i in merged["line_items"]] == [
        ("가구", "소계"), ("가구", "시공인건비"), ("가구", "식대"), ("가구", "운송비"),
        ("바닥", "소계"), ("바닥", "우드륨 2.2t"), ("바닥", "인건비"), ("바닥", "식대"),
    ]


def test_뒤에_설명이_붙은_소계도_소계_행으로_남는다():
    from pipeline.parsing import is_subtotal_row, merge_chunk_results

    items = [{"code": "1400", "category": "도배", "description": "소 계 (확장, 가벽 포함)", "amount": 3_354_000, "unit_price": 0},
             {"code": "", "category": "도배", "description": "실크벽지", "amount": 3_354_000, "unit_price": 10_000}]
    risk = merge_chunk_results([_chunk(items)], for_risk=True)["line_items"]
    assert [is_subtotal_row(i) for i in risk] == [True, False]


# ── 여러 장(쪽) 사이의 겹침 ──────────────────────────────────────────────


def test_쪽_사이에_한_줄만_겹쳐도_뺀다():
    from pipeline.parsing import drop_page_overlap
    previous = [_item("도배", "실크벽지", 900_000), _item("도배", "초배지", 100_000)]
    page = [_item("도배", "초배지", 100_000), _item("도배", "소계", 1_000_000)]
    assert drop_page_overlap(previous, page) == page[1:]


def test_쪽_사이에_금액만_우연히_같은_다른_줄은_빼지_않는다():
    from pipeline.parsing import drop_page_overlap
    previous = [_item("목공", "몰딩", 500_000), _item("목공", "공과잡비", 1_000_000)]
    page = [_item("철거", "철거", 500_000), _item("철거", "폐기물처리", 1_000_000), _item("철거", "소계", 1_500_000)]
    assert drop_page_overlap(previous, page) == page


def test_겹치지_않은_쪽의_같은_줄은_빼지_않는다():
    # 공종마다 반복되는 "인건비 300,000"이 두 쪽에 있어도 앞 쪽의 끝이 아니면 겹친 것이 아니다
    from pipeline.parsing import drop_page_overlap
    previous = [_item("전기", "인건비", 300_000), _item("전기", "소계", 500_000)]
    page = [_item("전기", "등기구", 400_000), _item("전기", "인건비", 300_000), _item("전기", "소계", 700_000)]
    assert drop_page_overlap(previous, page) == page


def test_겹친_줄은_공종과_소계_표기가_달리_읽혀도_뺀다():
    from pipeline.parsing import drop_page_overlap
    previous = [_item("가구", "신발장", 345_000), _item("가구", "소계", 1_345_000)]
    page = [_item("확장", "신발장", 345_000), _item("확장", "소 계 (확장포함)", 1_345_000), _item("확장", "창호", 2_000_000)]
    assert drop_page_overlap(previous, page) == page[2:]


def test_첫_쪽은_그대로_둔다():
    from pipeline.parsing import drop_page_overlap
    page = [_item("도배", "실크벽지", 900_000)]
    assert drop_page_overlap([], page) == page
