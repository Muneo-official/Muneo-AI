"""리스크 진단의 가격 이상 탐지 — 품목의 단가를 실제 견적서들의 단가와 비교한다.

예전에는 공종의 합계(가구 1,300만 원)를 비슷한 집 15곳의 합계와 비교했다. 집마다 공사 범위가 달라 합계는 원래
2배씩 차이 나서, "비싸게 부른 것"과 "많이 하는 것"을 구분하지 못했다 — 실제 견적서 100건 중 74건이 가격 지적을
받았고(견적서당 2.7개), 기준을 중간값의 2배까지 조여도 58건이 받았다.

단가는 집이 달라도 비슷하다. 도배 인건비는 하루 25~30만 원, 목공 인건비는 25~37만 원에 모여 있다(코퍼스 기준).
그래서 줄마다 단가를 같은 품목의 단가 범위와 비교하고, 한 공종에서 비교한 줄의 절반 이상이 같은 방향으로 벗어날
때만 그 공종을 지적한다. 같은 표본에서 견적서당 0.2개, 100건 중 18건으로 줄었다.

같은 이름의 품목도 단위가 다르면 다른 값이다. 도배 부자재는 "식"으로 적으면 12~40만 원, "㎡"로 적으면 1,500원이다.
단위를 가리지 않고 묶었을 때는 이 둘이 한 범위(1,500~400,000원)가 되어, 실제 견적서의 단가 지적 넷 중 하나가 그
범위 때문에 생긴 것이었다. 그래서 단위까지 같은 줄끼리만 비교한다.

기준표는 estimate_cases의 품목에서 만든다(scripts/build_unit_price_reference.py). 이 모듈은 DB를 모른다.
"""

import re
import statistics
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from app.domain.risk_models import RiskIssue

MIN_REQUESTS = 15       # 서로 다른 의뢰 이만큼에서 나온 품목만 기준으로 쓴다. 한 업체의 양식이 기준이 되지 않게
HIGH_RATIO = 1.3        # 상위 10%보다 높고, 중간값의 이 배수도 넘어야 "높음". 범위가 좁은 품목(식대 15,000)의 잔차이를 거른다
LOW_RATIO = 0.7         # 하위 10%보다 낮고, 중간값의 이 배수보다도 낮아야 "낮음"
MIN_COMPARED_LINES = 2  # 한 공종에서 단가를 비교한 줄이 이보다 적으면 지적하지 않는다. 한 줄로는 그 공종을 말할 수 없다
MIN_SHARE = 0.5         # 비교한 줄 중 이 비율 이상이 같은 방향으로 벗어나야 지적한다
MAX_EXAMPLES = 2
MAX_SPREAD = 5          # 상위 10%가 하위 10%의 이 배수 이상인 품목은 기준으로 쓰지 않는다. 범위가 이만큼 넓으면 "보통 얼마"를 말할 수 없다
# 수량 비교: 평수에 비례하는 단위로 적힌 품목만 본다. "식"·"개"·"M/D"는 집 크기와 따로 논다
MEASURED_UNITS = {"평", "m2", "자", "m"}
QUANTITY_RATIO = 1.5    # 평당 수량이 같은 품목의 상위 10%의 이 배수를 넘으면 "수량이 많음"
DOMINANT_UNIT_SHARE = 0.8  # 단위가 안 적힌 줄은, 그 품목의 줄 대부분이 한 단위로 적혀 있을 때만 그 단위의 기준과 비교한다
_UNIT_ALIASES = {
    "㎡": "m2", "m²": "m2", "제곱미터": "m2", "py": "평", "pyeong": "평", "미터": "m",
    "ea": "개", "세트": "set", "셋트": "set", "박스": "box",
    "m/d": "인", "명": "인", "품": "인",  # 하루 한 사람의 품
}
# 코퍼스에는 견적서의 구분 이름이 그대로 남은 줄이 있다("도배공사", "조명공사"). "공사"를 떼고 아래 이름으로 맞춘다
_CATEGORY_ALIASES = {"목": "목공", "조명": "전기", "수전": "설비", "도기": "욕실", "도기,수전": "욕실", "도기/수전": "욕실", "시트": "필름"}

# 같은 품목이 견적서에 따라 어느 쪽으로도 읽히는 공종은 한 묶음으로 본다(수전공사의 부속은 욕실과 설비에 반반)
_BUCKET = {"설비": "욕실"}
_PAREN_RE = re.compile(r"\(.*?\)|\[.*?\]")
_NOISE_RE = re.compile(r"[\d.,*×xX/\s:+\-~]+")
KEY_LENGTH = 12

Key = tuple[str, str, str]  # (공종 묶음, 품명, 단위)
QuantityKey = Key


def _to_int(value: Any) -> int:
    """모델이나 코퍼스의 금액을 정수로. 숫자가 아닌 값은 0 — 한 줄 때문에 진단 전체가 실패하면 안 된다."""
    try:
        return int(float(str(value).replace(",", ""))) if value not in (None, "") else 0
    except ValueError:
        return 0


def _category_of(item: dict[str, Any]) -> str:
    return str(item.get("category") or "")


def bucket(category: str) -> str:
    name = category.strip()
    name = name[:-2] if name.endswith("공사") else name
    name = _CATEGORY_ALIASES.get(name, name)
    return _BUCKET.get(name, name)


def item_key(item: dict[str, Any]) -> Key | None:
    """품목을 묶는 열쇠 (공종 묶음, 품명에서 괄호·숫자·기호를 뺀 앞 글자, 단위).

    "실크벽지(LX 베스띠, 개나리 로하스)"와 "실크벽지(LG,베스티)"는 같은 품목이다. 단위가 안 적힌 줄의 단위는 ""다.
    """
    name = _NOISE_RE.sub("", _PAREN_RE.sub("", str(item.get("description") or "")))[:KEY_LENGTH]
    category = bucket(str(item.get("category") or ""))
    return (category, name, normalize_unit(item.get("unit"))) if category and name else None


def normalize_unit(unit: Any) -> str:
    text = str(unit or "").strip().lower().replace(" ", "")
    return _UNIT_ALIASES.get(text, text)


def quantity_of(item: dict[str, Any]) -> float | None:
    """품목의 수량. 리스크 진단의 파싱은 수량을 내지 않으므로 금액 ÷ 단가로 구한다."""
    unit_price = usable_unit_price(item)
    return _to_int(item.get("amount")) / unit_price if unit_price else None


def quantity_key(item: dict[str, Any]) -> QuantityKey | None:
    key = item_key(item)
    return key if key and key[2] in MEASURED_UNITS else None


def usable_unit_price(item: dict[str, Any]) -> int | None:
    """비교에 쓸 수 있는 단가. 단가가 없거나 금액보다 큰 줄(열이 뒤바뀐 줄)은 쓰지 않는다."""
    unit_price, amount = _to_int(item.get("unit_price")), _to_int(item.get("amount"))
    return unit_price if 0 < unit_price <= amount else None


def build_reference(cases: list[dict[str, Any]], exclude_request: str | None = None) -> dict[Key, dict[str, int]]:
    """견적서들의 품목에서 단가 기준표를 만든다. 반환: 열쇠 → {n, p10, median, p90}.

    단위가 안 적힌 줄은 기준에 넣지 않는다. 대신 한 품목의 줄 대부분(DOMINANT_UNIT_SHARE)이 한 단위로 적혀 있으면
    그 단위의 기준을 단위 ""의 열쇠로도 넣어, 단위 칸이 없는 견적서의 줄을 그 기준과 비교할 수 있게 한다.

    exclude_request: 이 의뢰(request_url, 없으면 article_id)의 견적서는 뺀다. 채점할 때 견적서가 자기 자신의 단가와
    비교되지 않게 하는 데 쓴다.
    """
    prices: dict[Key, list[tuple[str, int]]] = defaultdict(list)
    for case in cases:
        if case.get("is_non_residential") is True:
            continue
        request = str(case.get("request_url") or case.get("article_id") or "")
        if exclude_request and request == exclude_request:
            continue
        for item in (case.get("parsed_estimate") or {}).get("line_items") or []:
            key, unit_price = item_key(item), usable_unit_price(item)
            if key and key[2] and unit_price:
                prices[key].append((request, unit_price))
    reference = {}
    lines: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for key, rows in prices.items():
        lines[key[:2]][key[2]] = len(rows)
        if len({request for request, _ in rows}) < MIN_REQUESTS:
            continue
        values = sorted(price for _, price in rows)
        n = len(values)
        if values[-(n // 10) - 1] >= values[n // 10] * MAX_SPREAD:
            continue
        reference[key] = {"n": n, "p10": values[n // 10], "median": int(statistics.median(values)), "p90": values[-(n // 10) - 1]}
    for (category, name), by_unit in lines.items():
        unit = max(by_unit, key=by_unit.get)
        if (category, name, unit) in reference and by_unit[unit] / sum(by_unit.values()) >= DOMINANT_UNIT_SHARE:
            reference[(category, name, "")] = reference[(category, name, unit)]
    return reference


def build_quantity_reference(cases: list[dict[str, Any]], exclude_request: str | None = None) -> dict[QuantityKey, dict[str, float]]:
    """견적서들의 품목에서 "평당 수량" 기준표를 만든다. 반환: (공종 묶음, 품명, 단위) → {n, median, p90}.

    평·㎡·자·m처럼 집 크기에 비례하는 단위의 품목만 넣는다. 평수를 모르는 견적서는 뺀다.
    """
    ratios: dict[QuantityKey, list[tuple[str, float]]] = defaultdict(list)
    for case in cases:
        pyeong = float(case.get("size_pyeong") or 0)
        if case.get("is_non_residential") is True or pyeong <= 0:
            continue
        request = str(case.get("request_url") or case.get("article_id") or "")
        if exclude_request and request == exclude_request:
            continue
        for item in (case.get("parsed_estimate") or {}).get("line_items") or []:
            key, quantity = quantity_key(item), quantity_of(item)
            if key and quantity:
                ratios[key].append((request, quantity / pyeong))
    reference = {}
    for key, rows in ratios.items():
        if len({request for request, _ in rows}) < MIN_REQUESTS:
            continue
        values = sorted(ratio for _, ratio in rows)
        n = len(values)
        reference[key] = {"n": n, "median": round(statistics.median(values), 3), "p90": round(values[-(n // 10) - 1], 3)}
    return reference


class UnitPriceReference:
    """단가·수량 기준표와, 견적서의 품목을 그 기준에 견주는 판정."""

    def __init__(self, table: dict[Key, dict[str, int]], quantities: dict[QuantityKey, dict[str, float]] | None = None):
        self._table = table
        self._quantities = quantities or {}

    def __len__(self) -> int:
        return len(self._table)

    def judge(self, item: dict[str, Any]) -> tuple[str | None, dict[str, int]] | None:
        """줄 하나의 단가 판정. 반환: ("높음"|"낮음"|None, 기준) — 비교할 기준이 없는 줄은 None."""
        key, unit_price = item_key(item), usable_unit_price(item)
        ref = self._table.get(key) if key and unit_price else None
        if ref is None:
            return None
        if unit_price > ref["p90"] and unit_price > ref["median"] * HIGH_RATIO:
            return "높음", ref
        if unit_price < ref["p10"] and unit_price < ref["median"] * LOW_RATIO:
            return "낮음", ref
        return None, ref

    def issues(self, line_items: list[dict[str, Any]], process_of: Callable[[dict], str] = _category_of) -> list[RiskIssue]:
        """공종마다, 단가를 비교한 줄의 절반 이상이 같은 방향으로 벗어나면 지적한다.

        process_of: 품목의 공종 이름을 주는 함수. 리스크 진단은 분석기의 공종 이름("전기/조명" 등)을 넘겨, 단가
        지적이 그 공종의 품목과 같은 자리에 나오게 한다.
        """
        judged: dict[str, list[tuple[str | None, dict, dict]]] = defaultdict(list)
        for item in line_items:
            result = self.judge(item)
            if result is not None:
                judged[process_of(item)].append((result[0], item, result[1]))
        issues = []
        for process, rows in judged.items():
            for direction in ("높음", "낮음"):
                off = [(item, ref) for d, item, ref in rows if d == direction]
                if len(rows) < MIN_COMPARED_LINES or len(off) / len(rows) < MIN_SHARE:
                    continue
                examples = ", ".join(
                    f"'{item.get('description', '')}' {usable_unit_price(item):,}원(보통 {ref['p10']:,}~{ref['p90']:,}원)"
                    for item, ref in off[:MAX_EXAMPLES]
                )
                issues.append(RiskIssue(
                    # 화면의 유형은 아직 누락·중복·불분명 셋이다. 가격을 별도 유형으로 나누는 것은 프론트와 맞춘 뒤에 한다
                    "불분명", process, f"{process} 단가가 시세보다 {direction}",
                    f"단가를 비교한 {len(rows)}개 품목 중 {len(off)}개가 다른 견적서들의 단가보다 {direction[:1]}습니다. 예: {examples}",
                    "자재 등급이나 시공 범위가 다른지, 단가 산정 근거를 업체에 확인하세요." if direction == "높음"
                    else "빠진 작업이 있거나 나중에 추가 비용이 붙는 것은 아닌지 업체에 확인하세요.",
                ))
        return issues

    def quantity_issues(self, line_items: list[dict[str, Any]], pyeong: int,
                        process_of: Callable[[dict], str] = _category_of) -> list[RiskIssue]:
        """평수에 비해 수량이 지나치게 많은 줄을 지적한다(20평 집에 도배 125평).

        같은 품목·같은 단위의 평당 수량과 비교한다. 상위 10%의 QUANTITY_RATIO배를 넘어야 지적한다 — 확장한 집,
        천장까지 하는 집처럼 수량이 많은 정상 견적서가 흔하다.
        """
        if pyeong <= 0:
            return []
        issues = []
        for item in line_items:
            key, quantity = quantity_key(item), quantity_of(item)
            ref = self._quantities.get(key) if key and quantity else None
            if ref is None or quantity / pyeong <= ref["p90"] * QUANTITY_RATIO:
                continue
            unit = normalize_unit(item.get("unit"))
            issues.append(RiskIssue(
                "불분명", process_of(item), "수량이 평수에 비해 많음",
                f"'{item.get('description', '')}' {quantity:g}{unit}은 {pyeong}평 집에 보통 들어가는 양"
                f"(약 {ref['median'] * pyeong:.0f}{unit}, 많아도 {ref['p90'] * pyeong:.0f}{unit})보다 많습니다.",
                "실측한 수량인지, 로스(여유분)를 얼마나 잡았는지 업체에 확인하세요.",
            ))
        return issues
