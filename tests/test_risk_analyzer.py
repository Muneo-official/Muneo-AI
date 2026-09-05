from app.domain.risk_analyzer import RiskAnalyzer


def test_missing_required_keyword_group_flagged():
    # 철거 공종인데 "폐기물" 관련 키워드가 전혀 없음 -> 누락 이슈
    line_items = [{"category": "철거공사", "description": "바닥 철거", "amount": 100000, "unit_price": 100000}]

    issues, processes = RiskAnalyzer().analyze(line_items)

    assert processes == ["철거"]
    assert any(i.type == "누락" for i in issues)


def test_all_required_groups_present_no_missing_issue():
    line_items = [
        {"category": "철거공사", "description": "바닥 철거", "amount": 100000, "unit_price": 100000},
        {"category": "철거공사", "description": "폐기물 처리", "amount": 50000, "unit_price": 50000},
    ]

    issues, _ = RiskAnalyzer().analyze(line_items)

    assert not any(i.type == "누락" for i in issues)


def test_duplicate_description_and_amount_flagged():
    line_items = [
        {"category": "도배공사", "description": "실크벽지 시공", "amount": 500000, "unit_price": 500000},
        {"category": "도배공사", "description": "실크벽지 시공", "amount": 500000, "unit_price": 500000},
    ]

    issues, _ = RiskAnalyzer().analyze(line_items)

    assert any(i.type == "중복" for i in issues)


def test_vague_keyword_in_description_flagged():
    line_items = [{"category": "목공사", "description": "몰딩 별도 협의", "amount": 200000, "unit_price": 200000}]

    issues, _ = RiskAnalyzer().analyze(line_items)

    assert any(i.type == "불분명" and "모호한 표현" in i.title for i in issues)


def test_missing_unit_price_or_amount_flagged():
    line_items = [{"category": "타일공사", "description": "욕실 타일", "amount": 0, "unit_price": None}]

    issues, _ = RiskAnalyzer().analyze(line_items)

    assert any(i.type == "불분명" and "단가 또는 금액" in i.detail for i in issues)


def test_aggregate_rows_excluded_from_analysis():
    # "소계"/"합계" 행은 필수항목 판단에서 제외되어야 함 -> 이 항목만 있으면 실질 항목이 없어
    # 여전히 누락 이슈가 나야 정상 (소계 행이 실제 항목으로 오인되면 안 됨)
    line_items = [{"category": "철거공사", "description": "소계", "amount": 100000, "unit_price": 100000}]

    issues, _ = RiskAnalyzer().analyze(line_items)

    assert any(i.type == "누락" for i in issues)


def test_unrecognized_category_and_description_dropped():
    line_items = [{"category": "창호공사", "description": "냉난방기 설치", "amount": 100000, "unit_price": 100000}]

    issues, processes = RiskAnalyzer().analyze(line_items)

    # 원본 analyzer.py의 기존 한계 그대로 유지: PROCESS_CATEGORY_MAP/PROCESS_KEYWORDS에
    # 없는 공종(창호 등)은 아예 그룹핑되지 않아 분석 대상에서 빠진다.
    assert processes == []
    assert issues == []
