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
