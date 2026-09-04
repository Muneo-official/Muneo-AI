from pipeline.categories import NORMALIZED_CATEGORIES
from pipeline.tool_schema import ESTIMATE_TOOL, TOOL_NAME


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
