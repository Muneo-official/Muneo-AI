"""line_items → cost_* 필드 집계.

pipeline/reference/build_rag.py의 build_category_costs()를 그대로 가져오되, category
정규화만 pipeline/categories.py의 normalize_category()(쉼표 복합 표기 분해 포함)로
바꿨다 — 원본 로직(1.3배 초과 시 비율 재산정)은 검증된 그대로 유지한다.
"""

from pipeline.categories import normalize_category


def build_category_costs(line_items: list[dict], total_cost: int) -> dict[str, int]:
    """line_items에서 정규화 카테고리별 금액 합산.

    대분류/소분류 중복 집계를 피하기 위해 line_items 합계가 total_cost 1.3배 이상이면
    total_cost 비율로 재산정한다(원본 로직 그대로).
    """
    raw: dict[str, int] = {}
    for item in line_items:
        norm = normalize_category(item.get("category", ""))
        if not norm:
            continue
        amount = int(item.get("amount") or 0)
        if amount > 0:
            raw[norm] = raw.get(norm, 0) + amount

    line_sum = sum(raw.values())
    if total_cost > 0 and line_sum > total_cost * 1.3:
        ratio = total_cost / line_sum
        raw = {k: int(v * ratio) for k, v in raw.items()}

    return {f"cost_{k}": v for k, v in raw.items()}
