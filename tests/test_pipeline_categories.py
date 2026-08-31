from pipeline.categories import normalize_category


def test_known_category_direct_lookup():
    assert normalize_category("도배공사") == "도배"


def test_compound_category_uses_first_recognized_token():
    assert normalize_category("목공,도어") == "목공"


def test_unrecognized_category_returns_none():
    assert normalize_category("냉난방공사") is None


def test_new_misc_expense_category_maps_correctly():
    # pipeline/prompts.py 규칙7에서 신설한 표준 카테고리 — "기타공사"의 실제 내용(승강기
    # 보양비, 주민동의서 대행료 등)이 부대비용이었다는 조사 결과를 반영
    assert normalize_category("기타/공과잡비") == "공과잡비"


def test_new_expansion_category_maps_correctly():
    assert normalize_category("확장공사") == "확장"


def test_typo_variants_of_window_category_map_to_window():
    assert normalize_category("현호공사") == "창호"
    assert normalize_category("철호공사") == "창호"
