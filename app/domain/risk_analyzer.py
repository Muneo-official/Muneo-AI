from collections import Counter, defaultdict
from typing import Any

from app.domain.risk_constants import PROCESS_CATEGORY_MAP, PROCESS_DISPLAY_NAME
from app.domain.risk_models import RiskIssue

AGGREGATE_KEYWORDS = ["소계", "합계", "총계", "공사비합계"]
PROCESS_KEYWORDS = {
    "철거": ["철거", "폐기물", "철거공사"],
    "설비": ["설비", "배관", "수전", "위생", "수도", "급수", "배수"],
    "전기/조명": ["전기", "조명", "등기구", "콘센트", "스위치", "배선", "분전반"],
    "목공": ["목공", "몰딩", "문틀", "걸레받이", "천장", "가벽"],
    "도배": ["도배", "벽지", "실크", "합지"],
    "바닥": ["바닥", "마루", "장판", "강마루", "수장"],
    "타일": ["타일", "방수", "줄눈"],
    "욕실": ["욕실", "양변기", "세면", "샤워", "도기", "욕조", "환풍기"],
    "주방": ["주방", "싱크", "후드", "상판"],
    "도장": ["도장", "페인트", "탄성코트", "도색"],
    "가구": ["가구", "붙박이", "신발장", "수납장", "키큰장", "장"],
}
# 필수 항목: 그 공사를 하면 반드시 따라오는 것만 둔다. 없으면 공사 중 추가 비용으로 돌아온다.
#   process — 지적을 붙일 공종
#   when    — 이 낱말 묶음이 모두 견적서에 있을 때만 본다(묶음 안에서는 하나만 있으면 된다). 비어 있으면 그 공종의
#             품목이 있을 때 본다
#   need    — 이 낱말 중 하나가 견적서 어디에든 있어야 한다
# 견적서 전체에서 찾는다. 방수를 욕실이 아니라 기타공사에, 폐기물 처리를 철거가 아니라 기타공사에 적는 견적서가 많다.
#
# 예전 표에는 하지 않아도 되는 공사가 필수로 들어 있었다(목공의 천장/가벽·문틀/도어, 설비의 배관, 전기의 콘센트,
# 주방의 상판/후드). 실제 견적서 368건의 98%가 "누락" 지적을 받았고, 그 대부분이 이 항목들이었다.
REQUIRED_ITEMS = [
    {"process": "철거", "when": [], "need": ["폐기물", "폐기"], "label": "폐기물 처리"},
    {"process": "욕실", "when": [["욕실", "화장실"], ["타일"]], "need": ["방수"], "label": "방수"},
]

# 섹션마다 한 번씩 나오는 게 정상인 줄. 품명과 금액이 같아도 중복으로 보지 않는다 — 도기공사와 수전공사의 "인건비
# 300,000"은 서로 다른 작업이다. 실제 견적서의 중복 지적 305건 대부분이 이런 줄이었다.
GENERIC_LINE_WORDS = ["인건비", "식대", "운송비", "운반비", "부자재", "장비대", "경비", "노무비"]

# 한 공종의 금액이 이 이상인데 품목이 수량 없는 한 줄("1식")뿐이면 "일괄 처리"로 지적한다. 무엇이 포함인지 알 수
# 없는 줄이다. 작은 공종(도장 한 줄 50만 원)은 한 줄이 자연스러워서 금액으로 거르고, "강마루 24평 × 125,000"처럼
# 수량과 단가가 적힌 한 줄은 내역이 있는 것이라 지적하지 않는다.
LUMP_SUM_MIN_AMOUNT = 2_000_000

# 견적서마다 나누는 방식이 달라 같은 품목이 어느 쪽에도 들어가는 공종 묶음. 이 안에서 같은 줄이 두 공종에 있는 것은
# 견적서의 중복이 아니라 분류의 흔들림일 수 있어, 다른 공종 중복으로 지적하지 않는다
SIBLING_PROCESSES = [{"욕실", "설비", "타일"}, {"주방", "가구"}, {"목공", "창호"}]

# 소계가 품목의 합과 이만큼 넘게 다르면 계산 오류로 지적한다. 원 단위 반올림 차이는 넘긴다
SUBTOTAL_TOLERANCE = 1_000


def _normalize_text(value: Any) -> str:
    return str(value or "").replace(" ", "").replace("/", "").replace("·", "").strip()


def _is_subtotal(item: dict[str, Any]) -> bool:
    return _normalize_text(item.get("description")) == "소계"


def _is_generic_line(description: str) -> bool:
    return any(word in description for word in GENERIC_LINE_WORDS)


def _is_aggregate_item(item: dict[str, Any]) -> bool:
    desc = _normalize_text(item.get("description"))
    if not desc:
        return False
    if desc in AGGREGATE_KEYWORDS:
        return True
    return desc.endswith("소계") or desc.endswith("합계") or desc.endswith("총계")


class RiskAnalyzer:
    # "기타"는 넣지 않는다. "부자재(백시멘트, 기타)"처럼 평범한 품명에 흔해서, 실제 견적서의 모호 표현 지적 842건 중
    # 827건이 이 낱말 하나 때문이었다.
    vague_keywords = [
        "별도",
        "협의",
        "추후",
        "미정",
        "현장상황",
        "동등품",
        "업체지정",
        "업체 지정",
        "임의",
        "확인필요",
        "확인 필요",
    ]

    def analyze(self, line_items: list[dict[str, Any]]) -> tuple[list[RiskIssue], list[str]]:
        issues: list[RiskIssue] = []
        by_process = self.group_by_process(line_items)
        detected_processes = sorted(by_process.keys())
        issues.extend(self._missing_required(line_items, by_process))
        issues.extend(self._cross_process_duplicates(by_process))
        issues.extend(self._subtotal_mismatches(line_items))

        for process, items in by_process.items():
            filtered_items = [i for i in items if not _is_aggregate_item(i)]

            dup_counter = Counter(
                (i.get("description", ""), int(i.get("amount") or 0))
                for i in filtered_items
                if i.get("description") and not _is_generic_line(i["description"])
            )
            for (desc, _), cnt in dup_counter.items():
                if cnt >= 2:
                    issues.append(RiskIssue("중복", process, "동일 항목 중복 기재", f"{desc} 항목이 {cnt}회 반복되었습니다.", "중복 계산 여부를 확인해 감액 가능한지 문의하세요."))

            if (len(filtered_items) == 1 and int(filtered_items[0].get("amount") or 0) >= LUMP_SUM_MIN_AMOUNT
                    and float(filtered_items[0].get("quantity") or 1) <= 1):
                only = filtered_items[0]
                issues.append(RiskIssue("불분명", process, "세부 내역 없이 일괄 금액", f"{PROCESS_DISPLAY_NAME.get(process, process)} 공사가 '{only.get('description', '')}' 한 줄({int(only['amount']):,}원)로만 적혀 있습니다.", "자재·수량·시공비가 각각 얼마인지 세부 내역을 요청하세요."))

            for item in filtered_items:
                desc = item.get("description", "")
                notes = item.get("notes", "")
                # 금액이 없는 줄은 총액에 들어가지 않은 비용이다. "별도"·"협의"가 적혀 있어도 지적은 하나만 한다.
                # 단가 칸이 비고 금액만 있는 줄("1식 1,200,000")은 흔한 표기라 지적하지 않는다
                if item.get("amount") in [0, None]:
                    issues.append(RiskIssue("불분명", process, "총액에 포함되지 않은 항목", f"'{desc}' 항목은 금액이 적혀 있지 않아 견적 총액에 들어 있지 않습니다.", "이 작업을 하는지, 한다면 얼마인지 견적서에 금액으로 적어 달라고 요청하세요."))
                elif any(k in desc or k in notes for k in self.vague_keywords):
                    issues.append(RiskIssue("불분명", process, "모호한 표현 포함", f"'{desc}' 항목에 모호한 표현이 있습니다.", "세부 사양과 포함 범위를 명확히 요청하세요."))

        return issues, detected_processes

    @staticmethod
    def _cross_process_duplicates(by_process: dict[str, list]) -> list[RiskIssue]:
        """품명과 금액이 같은 줄이 서로 다른 공종에 있으면 중복으로 지적한다(폐기물 처리를 철거와 기타에 넣은 경우 등).

        인건비·식대처럼 공종마다 따로 드는 줄은 보지 않는다.
        """
        where: dict[tuple[str, int], list[str]] = defaultdict(list)
        for process, items in by_process.items():
            for key in {(i.get("description", ""), int(i.get("amount") or 0)) for i in items if not _is_aggregate_item(i)}:
                if key[0] and key[1] > 0 and not _is_generic_line(key[0]):
                    where[key].append(process)
        return [
            RiskIssue("중복", processes[-1], "다른 공종에 같은 항목", f"{desc} 항목({amount:,}원)이 {'·'.join(processes)}에 각각 들어 있습니다.", "같은 작업이 두 공종에 이중으로 계산된 것은 아닌지 확인하세요.")
            for (desc, amount), processes in where.items()
            if len(processes) >= 2 and not any(set(processes) <= siblings for siblings in SIBLING_PROCESSES)
        ]

    def _subtotal_mismatches(self, line_items: list[dict[str, Any]]) -> list[RiskIssue]:
        """견적서에 적힌 소계가 그 구분의 품목 합과 다르면 지적한다.

        소계 행은 파싱이 품목과 같은 순서로 넘긴다. 소계가 품목들 앞에 오는 양식과 뒤에 오는 양식이 있어서, 첫 행이
        소계면 "소계 다음의 품목들", 아니면 "소계 앞의 품목들"을 그 소계의 품목으로 본다.
        """
        rows = [i for i in line_items if _is_subtotal(i) or int(i.get("amount") or 0) > 0]
        if not rows or not any(_is_subtotal(i) for i in rows):
            return []
        groups: list[tuple[dict, list[dict]]] = []
        if _is_subtotal(rows[0]):
            for row in rows:
                if _is_subtotal(row):
                    groups.append((row, []))
                else:
                    groups[-1][1].append(row)
        else:
            pending: list[dict] = []
            for row in rows:
                if _is_subtotal(row):
                    groups.append((row, pending))
                    pending = []
                else:
                    pending.append(row)
        issues = []
        for subtotal_row, items in groups:
            written = int(subtotal_row.get("amount") or 0)
            summed = sum(int(i.get("amount") or 0) for i in items)
            if not items or written <= 0 or abs(written - summed) <= SUBTOTAL_TOLERANCE:
                continue
            process = self._infer_process(items[0]) or self._infer_process(subtotal_row) or str(subtotal_row.get("category") or "")
            issues.append(RiskIssue("불분명", process, "소계가 품목 합과 다름", f"{PROCESS_DISPLAY_NAME.get(process, process)} 소계는 {written:,}원인데 품목을 더하면 {summed:,}원입니다({written - summed:+,}원).", "소계에 견적서에 보이지 않는 금액이 들어 있는지, 계산이 틀린 것인지 확인하세요."))
        return issues

    @staticmethod
    def _missing_required(line_items: list[dict[str, Any]], by_process: dict[str, list]) -> list[RiskIssue]:
        """필수 항목(REQUIRED_ITEMS)이 견적서 어디에도 없으면 누락으로 지적한다."""
        text = _normalize_text(" ".join(
            f"{i.get('category') or ''} {i.get('description') or ''} {i.get('notes') or ''}"
            for i in line_items if not _is_aggregate_item(i)
        ))
        issues = []
        for rule in REQUIRED_ITEMS:
            applies = all(any(w in text for w in group) for group in rule["when"]) if rule["when"] else rule["process"] in by_process
            if applies and not any(w in text for w in rule["need"]):
                process = rule["process"]
                issues.append(RiskIssue("누락", process, f"{PROCESS_DISPLAY_NAME[process]} 필수 항목 누락", f"'{rule['label']}' 관련 항목이 견적서에서 확인되지 않습니다.", "업체에 해당 항목이 견적에 포함되어 있는지, 별도 비용인지 확인하세요."))
        return issues

    def group_by_process(self, line_items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        grouped = defaultdict(list)
        for item in line_items:
            process = self._infer_process(item)
            if process:
                grouped[process].append(item)
        return grouped

    def _infer_process(self, item: dict[str, Any]) -> str | None:
        category = _normalize_text(item.get("category"))
        description = _normalize_text(item.get("description"))
        searchable = f"{category} {description}"

        for process, categories in PROCESS_CATEGORY_MAP.items():
            normalized_categories = [_normalize_text(c) for c in categories]
            if category in normalized_categories or any(c and c in category for c in normalized_categories):
                return process

        for process, keywords in PROCESS_KEYWORDS.items():
            if any(_normalize_text(keyword) in searchable for keyword in keywords):
                return process
        return None
