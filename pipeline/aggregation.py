"""line_items → cost_*/has_* 필드 집계.

pipeline/reference/build_rag.py의 build_category_costs()/check_has_keywords()를
이관하되, category 정규화만 pipeline/categories.py의 normalize_category()(쉼표 복합
표기 분해 포함)로 바꿨다 — 원본 로직(1.3배 초과 시 비율 재산정, 키워드 매칭)은 검증된
그대로 유지한다.
"""

import re
from collections.abc import Callable

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


# ── 도어 분리 ──────────────────────────────────────────────────────────────
# 견적서 양식에 따라 도어(방문·중문·현관문)가 "창호공사"에 들어가기도 하고 "목공, 도어"에 들어가기도 한다.
# 분류를 그대로 따르면 cost_목공에 "도어가 든 목공"과 "도어가 없는 목공"이, cost_창호에 "샷시만"·"도어만"·
# "샷시+도어"가 섞인다(샷시류가 있는 사례의 창호 중앙값 1,055만 원, 도어류만 있는 사례 310만 원). 그래서
# 목공·창호로 분류된 품목 중 도어를 품명으로 골라 별도 공종 "도어"로 모은다.
DOOR_CATEGORY = "도어"
_DOOR_SOURCE_CATEGORIES = ("목공", "창호")

# 발코니 쪽 문(터닝도어·폴딩도어·발코니도어)은 샷시 업체가 시공하는 창호 제품이라 도어로 보지 않는다
_SASH_DOOR_RE = re.compile(r"터닝|타닝|터닐|발코니|베란다|폴딩|샷시|샤시|새시|분합")
# 문짝과 그 문틀·문선, 손잡이·경첩·도어록 같은 부속까지 도어다. 부속을 빼면 부속만 창호에 남아 "샷시 금액
# 10만 원"짜리 사례가 생긴다. 종문·동문·줄문·문문 등은 "중문"의 OCR 오인식이다.
_DOOR_RE = re.compile(
    r"도어|도아|door|문짝|문틀|문선|중문|현관문|현관출문|방문|목문|방화문|ABS|"
    r"손잡이|경첩|스토퍼|잠금장치|지문인|자문인|한샘문|"
    r"(접이|미닫이|여닫이|여달이|유리|자동|출입|판넬|전실)문|"
    r"현관.{0,10}(종문|동문|줄문|문문|용문|홀문|연문|3연동)|"
    r"(^|[\s+(])문([\s+)]|$)",
    re.IGNORECASE,
)
# 창호 안에서 샷시로 보는 품목. 여기에도 도어에도 안 걸리는 품목(인건비·식대·운송비·부자재)이 "일반 품목"이다
_SASH_RE = re.compile(
    r"창호|이중창|단창|중창|확장창|완성창|창문|창틀|샷시|샤시|새시|발코니|베란다|터닝|타닝|터닐|폴딩|분합|"
    r"방충망|KCC|LX|하이샤시|유리|sin|dou",
    re.IGNORECASE,
)


def is_door_item(description: str) -> bool:
    """품명이 도어(방문·중문·현관문과 그 부속)인지. 목공·창호로 분류된 품목에만 쓴다."""
    text = description or ""
    return bool(_DOOR_RE.search(text)) and not _SASH_DOOR_RE.search(text)


def category_amounts(line_items: list[dict], normalize: Callable[[str], str | None] = normalize_category) -> dict[str, float]:
    """품목을 공종별로 더한다. 목공·창호 안의 도어는 "도어"로 옮긴다. 금액은 옮기기만 하고 만들거나 없애지 않는다.

    창호 안의 일반 품목(인건비·식대·운송비 등)은 그 견적의 창호에 샷시가 없으면 전부 도어로, 샷시와 도어가
    함께 있으면 두 금액의 비율로 나눈다 — 그대로 두면 도어만 한 견적에 샷시 금액이 남는다. 목공 안의 일반
    품목은 목공에 둔다.

    normalize: 품목의 category를 정규화 공종으로 바꾸는 함수. 리스크 진단의 품목은 이미 정규화돼 있다.
    """
    sums: dict[str, float] = {}
    window_door = window_sash = window_generic = 0.0
    for item in line_items:
        category = normalize(item.get("category", "") or "")
        amount = int(item.get("amount") or 0)
        if not category or amount <= 0:
            continue
        description = item.get("description") or ""
        if category == "창호":
            if is_door_item(description):
                window_door += amount
            elif _SASH_RE.search(description):
                window_sash += amount
            else:
                window_generic += amount
        elif category == "목공" and is_door_item(description):
            sums[DOOR_CATEGORY] = sums.get(DOOR_CATEGORY, 0) + amount
        else:
            sums[category] = sums.get(category, 0) + amount

    if window_door or window_sash or window_generic:
        if window_sash <= 0:
            door_part, sash_part = window_door + window_generic, 0.0
        else:
            share = window_sash / (window_sash + window_door)
            sash_part = window_sash + window_generic * share
            door_part = window_door + window_generic * (1 - share)
        if sash_part > 0:
            sums["창호"] = sash_part
        if door_part > 0:
            sums[DOOR_CATEGORY] = sums.get(DOOR_CATEGORY, 0) + door_part
    return sums


def build_category_costs(line_items: list[dict], total_cost: int) -> dict[str, int]:
    """line_items에서 정규화 카테고리별 금액 합산. 목공·창호 안의 도어는 cost_도어로 분리한다.

    대분류/소분류 중복 집계를 피하기 위해 line_items 합계가 total_cost 1.3배 이상이면
    total_cost 비율로 재산정한다(원본 로직 그대로).

    cost_도어는 도어가 없어도 0으로 넣는다 — 이 필드가 있는지로 "도어를 분리한 기준으로 집계된 사례인지"를
    구분한다(재집계 전의 사례에는 이 필드가 없고 cost_목공·cost_창호에 도어가 섞여 있다).
    """
    raw = category_amounts(line_items)

    line_sum = sum(raw.values())
    if total_cost > 0 and line_sum > total_cost * 1.3:
        ratio = total_cost / line_sum
        raw = {k: v * ratio for k, v in raw.items()}

    costs = {f"cost_{k}": int(v) for k, v in raw.items() if int(v) > 0}
    costs.setdefault(f"cost_{DOOR_CATEGORY}", 0)
    return costs
