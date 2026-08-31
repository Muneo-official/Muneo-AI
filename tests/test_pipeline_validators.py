from pipeline.schemas import ParsedEstimate
from pipeline.validators import (
    suggest_door_reclassification,
    validate_case,
    validate_known_categories,
    validate_size_pyeong,
    validate_total_consistency,
)


def _line_item(category: str, description: str, amount: int) -> dict:
    return {"category": category, "description": description, "amount": amount}


def test_size_pyeong_in_range_has_no_issue():
    assert validate_size_pyeong(33) == []


def test_size_pyeong_out_of_range_flags_error():
    # 실제 관측된 오염 패턴(docs/IMPLEMENTATION_LOG.md 2-4) — article_id 숫자가 size_pyeong에 들어감
    issues = validate_size_pyeong(877930)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert issues[0].rule == "size_pyeong_range"


def test_total_consistency_within_tolerance_has_no_issue():
    parsed = ParsedEstimate.model_validate({
        "total_cost": 1_000_000,
        "line_items": [_line_item("도배공사", "실크벽지", 900_000)],
    })
    assert validate_total_consistency(parsed) == []


def test_total_consistency_exceeding_tolerance_flags_warning():
    parsed = ParsedEstimate.model_validate({
        "total_cost": 1_000_000,
        "line_items": [_line_item("도배공사", "실크벽지", 500_000)],
    })
    issues = validate_total_consistency(parsed)
    assert len(issues) == 1
    assert issues[0].severity == "warning"


def test_unknown_category_majority_of_cost_flags_error():
    parsed = ParsedEstimate.model_validate({
        "total_cost": 100_000,
        "line_items": [_line_item("냉난방공사", "에어컨 설치", 100_000)],
    })
    issues = validate_known_categories(parsed)
    assert len(issues) == 1
    assert issues[0].severity == "error"
    assert "냉난방공사" in issues[0].message


def test_unknown_category_small_ratio_of_cost_has_no_issue():
    # 실제 데이터로 확인된 문제: "기타공사"/"단가참고" 같은 사소한 미분류 항목이 몇 개
    # 섞였다고 케이스 전체를 못 미덥게 볼 필요는 없다 — 금액 비중이 작으면(여기선 5%)
    # 이슈로 잡지 않아야 한다 (개수 기반 페널티였던 이전 로직에서는 이것도 걸렸음).
    parsed = ParsedEstimate.model_validate({
        "total_cost": 1_000_000,
        "line_items": [
            _line_item("도배공사", "실크벽지", 950_000),
            _line_item("단가참고", "OOO 기준", 50_000),
        ],
    })
    assert validate_known_categories(parsed) == []


def test_door_category_furniture_keyword_suggests_reclassification():
    # 실제 사례(article_id=890396, eval/results/pricing_gap_diagnostic.md)와 동일한 패턴 —
    # "도어공사"로 분류됐지만 실제로는 가구(붙박이장) 문짝 교체
    parsed = ParsedEstimate.model_validate({
        "total_cost": 1_000_000,
        "line_items": [_line_item("도어공사", "안방 붙박이장 문짝교체", 1_000_000)],
    })
    suggestions = suggest_door_reclassification(parsed)
    assert len(suggestions) == 1
    assert suggestions[0]["suggested_category"] == "가구공사"


def test_door_category_without_furniture_keyword_not_suggested():
    parsed = ParsedEstimate.model_validate({
        "total_cost": 500_000,
        "line_items": [_line_item("도어공사", "현관문 ABS도어 교체", 500_000)],
    })
    assert suggest_door_reclassification(parsed) == []


def test_validate_case_combines_all_rules():
    result = validate_case(
        raw_parsed_estimate={
            "total_cost": 1_000_000,
            "line_items": [_line_item("도어공사", "안방 붙박이장 문짝교체", 1_000_000)],
        },
        size_pyeong=32,
    )
    assert not result.has_errors
    assert len(result.reclassification_suggestions) == 1
    assert result.confidence == 1.0


def test_validate_case_schema_failure_returns_low_confidence():
    result = validate_case(raw_parsed_estimate={"total_cost": 0, "line_items": []}, size_pyeong=32)
    assert result.has_errors
    assert result.confidence < 1.0
