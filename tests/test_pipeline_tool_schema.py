from pipeline.categories import NORMALIZED_CATEGORIES
from pipeline.tool_schema import (
    ESTIMATE_TOOL,
    RISK_ESTIMATE_TOOL,
    RISK_FAUCET_CATEGORY_RULE,
    RISK_FILM_CATEGORY_RULE,
    RISK_TOILET_CATEGORY_RULE,
    TOOL_NAME,
)


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


def _item_required(tool):
    return tool["input_schema"]["properties"]["line_items"]["items"]["required"]


def test_collection_schema_keeps_all_item_fields():
    # 크롤링 수집(배치)은 가견적 코퍼스가 되므로 필드를 줄이지 않는다 — 리스크 전용 스키마를 만들며 건드리지 않았는지 고정
    assert set(_item_properties(ESTIMATE_TOOL)) == {
        "code", "category", "description", "unit_price", "quantity", "unit", "amount",
    }
    assert _item_properties(ESTIMATE_TOOL)["code"] == {"type": "string"}
    assert _item_required(ESTIMATE_TOOL) == ["category", "description", "amount"]


def test_risk_schema_drops_quantity_requires_code_and_adds_film_rule():
    # unit은 다시 받는다 — 수량이 평인지 ㎡인지는 금액 ÷ 단가로 알 수 없다. quantity는 계산되므로 계속 뺀다
    assert set(_item_properties(ESTIMATE_TOOL)) - set(_item_properties(RISK_ESTIMATE_TOOL)) == {"quantity"}
    # code는 필수 + 채우는 규칙 설명 — 청크마다 code 유무가 달라 겹침 중복이 새던 문제 대응
    assert _item_required(RISK_ESTIMATE_TOOL) == ["code", "category", "description", "amount"]
    assert _item_properties(RISK_ESTIMATE_TOOL)["code"]["type"] == "string"
    assert "빈 문자열" in _item_properties(RISK_ESTIMATE_TOOL)["code"]["description"]
    # category: enum은 같고, 설명은 수집 스키마 설명 뒤에 필름 규칙만 붙는다
    risk_category = _item_properties(RISK_ESTIMATE_TOOL)["category"]
    collection_category = _item_properties(ESTIMATE_TOOL)["category"]
    assert risk_category["enum"] == collection_category["enum"]
    assert risk_category["description"] == (
        f"{collection_category['description']} {RISK_FILM_CATEGORY_RULE} {RISK_FAUCET_CATEGORY_RULE} "
        f"{RISK_TOILET_CATEGORY_RULE}"
    )
    # 분류 규칙은 리스크 스키마에만 — 수집 스키마는 그대로
    for rule in (RISK_FILM_CATEGORY_RULE, RISK_FAUCET_CATEGORY_RULE, RISK_TOILET_CATEGORY_RULE):
        assert rule not in collection_category["description"]
    # 나머지(description·amount·unit_price, 도구 이름·설명, total_cost)는 수집 스키마와 같아야 한다
    for field, spec in _item_properties(RISK_ESTIMATE_TOOL).items():
        if field not in ("code", "category"):
            assert spec == _item_properties(ESTIMATE_TOOL)[field]
    risk_schema = {k: v for k, v in RISK_ESTIMATE_TOOL["input_schema"]["properties"].items() if k != "line_items"}
    collection_schema = {k: v for k, v in ESTIMATE_TOOL["input_schema"]["properties"].items() if k != "line_items"}
    assert risk_schema == collection_schema
    assert RISK_ESTIMATE_TOOL["input_schema"]["required"] == ESTIMATE_TOOL["input_schema"]["required"]
    assert (RISK_ESTIMATE_TOOL["name"], RISK_ESTIMATE_TOOL["description"]) == (TOOL_NAME, ESTIMATE_TOOL["description"])


def test_리스크_지시문은_소계_행과_금액_없는_행을_받고_수집_지시문은_그대로다():
    from pipeline.tool_schema import RISK_TOOL_USE_INSTRUCTIONS, TOOL_USE_INSTRUCTIONS

    assert "집계 행은 반드시 제외" in TOOL_USE_INSTRUCTIONS and "소계 행은 line_items에 포함" not in TOOL_USE_INSTRUCTIONS
    assert "집계 행은 반드시 제외" not in RISK_TOOL_USE_INSTRUCTIONS
    assert "소계 행은 line_items에 포함" in RISK_TOOL_USE_INSTRUCTIONS and '"별도"' in RISK_TOOL_USE_INSTRUCTIONS
    # 나머지 단락(표 고르기, 열 구분, 잘린 이미지)은 같다
    assert "상세 테이블이 보이면 반드시 상세 테이블만 파싱한다" in RISK_TOOL_USE_INSTRUCTIONS
