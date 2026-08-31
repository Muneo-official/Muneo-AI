from pipeline.parsing import merge_and_validate, merge_parsed_results


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
