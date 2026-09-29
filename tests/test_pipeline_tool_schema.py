from pipeline.categories import NORMALIZED_CATEGORIES
from pipeline.tool_schema import ESTIMATE_TOOL, RISK_ESTIMATE_TOOL, TOOL_NAME


def test_tool_name_matches_constant():
    assert ESTIMATE_TOOL["name"] == TOOL_NAME


def test_category_enum_matches_normalized_categories_exactly():
    # 하드코딩된 별도 목록이 아니라 pipeline/categories.py의 단일 소스에서 나온 값이어야
    # 한다 — 나중에 카테고리가 추가돼도 두 군데를 따로 안 고치게.
    enum = ESTIMATE_TOOL["input_schema"]["properties"]["line_items"]["items"]["properties"]["category"]["enum"]
    assert set(enum) == set(NORMALIZED_CATEGORIES)


def test_required_fields_present():
    schema = ESTIMATE_TOOL["input_schema"]
    assert "is_estimate" in schema["required"]
    item_schema = schema["properties"]["line_items"]["items"]
    assert set(item_schema["required"]) == {"category", "description", "amount"}


def _item_properties(tool):
    return tool["input_schema"]["properties"]["line_items"]["items"]["properties"]


def test_collection_schema_keeps_all_item_fields():
    # 크롤링 수집(배치)은 가견적 코퍼스가 되므로 필드를 줄이지 않는다 — 리스크 전용 스키마를 만들며 건드리지 않았는지 고정
    assert set(_item_properties(ESTIMATE_TOOL)) == {
        "code", "category", "description", "unit_price", "quantity", "unit", "amount",
    }


def test_risk_schema_drops_only_unit_and_quantity():
    assert set(_item_properties(ESTIMATE_TOOL)) - set(_item_properties(RISK_ESTIMATE_TOOL)) == {"unit", "quantity"}
    # 나머지(카테고리 enum·설명, 필수 필드, 도구 이름·설명, total_cost)는 수집 스키마와 같아야 한다
    for field, spec in _item_properties(RISK_ESTIMATE_TOOL).items():
        assert spec == _item_properties(ESTIMATE_TOOL)[field]
    risk_schema = {k: v for k, v in RISK_ESTIMATE_TOOL["input_schema"]["properties"].items() if k != "line_items"}
    collection_schema = {k: v for k, v in ESTIMATE_TOOL["input_schema"]["properties"].items() if k != "line_items"}
    assert risk_schema == collection_schema
    assert RISK_ESTIMATE_TOOL["input_schema"]["required"] == ESTIMATE_TOOL["input_schema"]["required"]
    assert RISK_ESTIMATE_TOOL["input_schema"]["properties"]["line_items"]["items"]["required"] == [
        "category", "description", "amount",
    ]
    assert (RISK_ESTIMATE_TOOL["name"], RISK_ESTIMATE_TOOL["description"]) == (TOOL_NAME, ESTIMATE_TOOL["description"])
