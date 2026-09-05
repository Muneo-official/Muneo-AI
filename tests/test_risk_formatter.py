from app.domain.risk_formatter import ResponseFormatter
from app.domain.risk_models import RiskIssue


def _build(line_items=None, issues=None, requested_processes=None):
    return ResponseFormatter().build(
        company_name="홍길동 인테리어",
        space_type="아파트",
        pyeong=30,
        room_count=3,
        floor=8,
        elevator=True,
        region="서울",
        building_age="20년이상",
        line_items=line_items or [],
        issues=issues or [],
        requested_processes=requested_processes or [],
    )


def test_report_shell_fields():
    result = _build()

    report = result["report"]
    assert report["subtitle_fields"]["company_name"] == "홍길동 인테리어"
    assert report["construction_info"]["floor"] == 8
    assert report["summary"]["total_risk_items"] == 0


def test_issue_counts_reflected_in_cards_and_summary():
    issues = [
        RiskIssue("누락", "철거", "제목1", "상세1", "가이드1"),
        RiskIssue("중복", "도배", "제목2", "상세2", "가이드2"),
        RiskIssue("불분명", "도배", "제목3", "상세3", "가이드3"),
        RiskIssue("불분명", "도배", "제목4", "상세4", "가이드4"),
    ]

    result = _build(issues=issues, requested_processes=["철거", "도배"])

    cards = result["report"]["cards"]
    assert cards["missing"]["count"] == 1
    assert cards["duplicate"]["count"] == 1
    assert cards["unclear"]["count"] == 2
    assert result["report"]["summary"]["total_risk_items"] == 4
    assert result["report"]["summary"]["chips"] == {"누락": 1, "중복": 1, "불분명": 2}


def test_process_sections_group_normal_items_and_issues_by_process():
    line_items = [{"category": "도배공사", "description": "실크벽지", "amount": 500000, "notes": ""}]
    issues = [RiskIssue("불분명", "도배", "모호한 표현 포함", "상세", "가이드")]

    result = _build(line_items=line_items, issues=issues, requested_processes=["도배"])

    sections = result["report"]["process_sections"]
    assert len(sections) == 1
    assert sections[0]["process"] == "도배"
    statuses = [item["status"] for item in sections[0]["items"]]
    assert "정상" in statuses
    assert "불분명" in statuses


def test_normal_items_shown_for_pipeline_normalized_category_names():
    # pipeline/parsing.py(현재 파서)는 "-공사" 접미사 없는 짧은 카테고리(예: "철거", "설비")를
    # 낸다. PROCESS_CATEGORY_MAP이 접미사 붙은 표기만 알면 _process_from_category()가
    # 매칭에 실패해 정상 항목이 리포트에서 통째로 빠진다 - 실제 이미지 테스트로 발견한 회귀.
    line_items = [
        {"category": "철거", "description": "폐기물 처리", "amount": 300000, "notes": ""},
        {"category": "설비", "description": "배관 교체", "amount": 400000, "notes": ""},
    ]

    result = _build(line_items=line_items, requested_processes=["철거", "설비"])

    sections = {s["process"]: s["items"] for s in result["report"]["process_sections"]}
    assert [i["status"] for i in sections["철거"]] == ["정상"]
    assert [i["status"] for i in sections["설비"]] == ["정상"]


def test_sections_only_built_for_requested_processes():
    line_items = [{"category": "도배공사", "description": "실크벽지", "amount": 500000, "notes": ""}]

    result = _build(line_items=line_items, requested_processes=["철거"])

    sections = result["report"]["process_sections"]
    assert len(sections) == 1
    assert sections[0]["process"] == "철거"
    assert sections[0]["items"] == []
