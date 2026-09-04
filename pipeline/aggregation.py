"""line_items → cost_*/has_* 필드 집계.

pipeline/reference/build_rag.py의 build_category_costs()/check_has_keywords()를
이관하되, category 정규화만 pipeline/categories.py의 normalize_category()(쉼표 복합
표기 분해 포함)로 바꿨다 — 원본 로직(1.3배 초과 시 비율 재산정, 키워드 매칭)은 검증된
그대로 유지한다.
"""

from pipeline.categories import normalize_category

# has_* 판단 키워드. app/domain/estimate_engine.py가 실제로 읽는 has_* 필드 이름과
# 반드시 일치해야 한다 — 여기서 없는 값이 하나라도 빠지면 참고 사례 검색(Stage 1~4
# 점진적 필터)이 그 공종에 대해서만 조용히 항상 실패한다(과거 실제로 겪은 버그,
# docs/IMPLEMENTATION_LOG.md 2-12의 Atlas Vector Search Index 사례와 같은 종류).
HAS_KEYWORDS: dict[str, list[str]] = {
    "has_창호": ["창호", "샷시", "새시", "현관문", "도어", "발코니창"],
    "has_도배": ["도배", "벽지", "합지", "실크"],
    "has_타일": ["타일", "도기", "줄눈"],
    "has_가구": ["가구", "붙박이", "신발장", "싱크대", "수납장"],
    "has_욕실": ["욕실", "화장실", "욕조", "세면대", "변기"],
    "has_바닥": ["바닥", "마루", "장판", "데코타일", "강마루", "강화마루"],
    "has_전기": ["전기", "배선", "콘센트", "인덕션", "분전반"],
    "has_조명": ["조명", "다운라이트", "간접등", "LED", "등기구"],
}


def build_check_text(request_body_text: str, line_items: list[dict]) -> str:
    """has_* 판단용 텍스트: 요청 원문 + line_items의 category·description 전부 합산."""
    parts = [request_body_text or ""]
    for item in line_items:
        if item.get("category"):
            parts.append(item["category"])
        if item.get("description"):
            parts.append(item["description"])
    return " ".join(parts)


def build_has_flags(request_body_text: str, line_items: list[dict]) -> dict[str, str]:
    """ChromaDB/Mongo 관례를 따라 bool 대신 "true"/"false" 문자열로 반환한다."""
    text = build_check_text(request_body_text, line_items)
    return {
        key: "true" if any(kw in text for kw in keywords) else "false"
        for key, keywords in HAS_KEYWORDS.items()
    }


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
