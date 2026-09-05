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
REQUIRED_KEYWORD_GROUPS = {
    "철거": [["철거"], ["폐기물", "폐기"]],
    "설비": [["배관", "급수", "배수", "수도"], ["수전", "위생"]],
    "전기/조명": [["전기", "배선"], ["조명", "등기구", "전등"], ["콘센트", "스위치"]],
    "목공": [["목공", "몰딩", "걸레받이"], ["문틀", "도어"], ["천장", "가벽"]],
    "도배": [["벽지", "도배"]],
    "바닥": [["장판", "마루", "바닥"]],
    "타일": [["타일", "방수", "줄눈"]],
    "욕실": [["양변기", "세면", "샤워", "욕실", "도기"], ["방수", "배수"]],
    "주방": [["싱크", "주방"], ["상판", "후드"]],
    "도장": [["도장", "페인트", "탄성코트", "도색"]],
    "가구": [["가구", "붙박이", "신발장", "수납장", "키큰장"]],
}


def _normalize_text(value: Any) -> str:
    return str(value or "").replace(" ", "").replace("/", "").replace("·", "").strip()


def _is_aggregate_item(item: dict[str, Any]) -> bool:
    desc = _normalize_text(item.get("description"))
    if not desc:
        return False
    if desc in AGGREGATE_KEYWORDS:
        return True
    return desc.endswith("소계") or desc.endswith("합계") or desc.endswith("총계")


class RiskAnalyzer:
    vague_keywords = [
        "별도",
        "협의",
        "추후",
        "미정",
        "현장상황",
        "기타",
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

        for process, items in by_process.items():
            filtered_items = [i for i in items if not _is_aggregate_item(i)]
            required_groups = REQUIRED_KEYWORD_GROUPS.get(process, [])
            search_text = _normalize_text(
                " ".join(
                    " ".join(
                        str(i.get(field) or "")
                        for field in ["category", "description", "notes"]
                    )
                    for i in filtered_items
                )
            )
            for group in required_groups:
                if not any(_normalize_text(key) in search_text for key in group):
                    missing_label = "/".join(group)
                    issues.append(RiskIssue("누락", process, f"{PROCESS_DISPLAY_NAME[process]} 필수 항목 누락", f"'{missing_label}' 관련 항목이 견적서에서 확인되지 않습니다.", "업체에 해당 공종의 필수 세부 항목 포함 여부를 확인하세요."))
                    break

            dup_counter = Counter((i.get("description", ""), int(i.get("amount") or 0)) for i in filtered_items if i.get("description"))
            for (desc, _), cnt in dup_counter.items():
                if cnt >= 2:
                    issues.append(RiskIssue("중복", process, "동일 항목 중복 기재", f"{desc} 항목이 {cnt}회 반복되었습니다.", "중복 계산 여부를 확인해 감액 가능한지 문의하세요."))

            for item in filtered_items:
                desc = item.get("description", "")
                notes = item.get("notes", "")
                if any(k in desc or k in notes for k in self.vague_keywords):
                    issues.append(RiskIssue("불분명", process, "모호한 표현 포함", f"'{desc}' 항목에 모호한 표현이 있습니다.", "세부 사양과 포함 범위를 명확히 요청하세요."))
                if (item.get("unit_price") in [0, None]) or (item.get("amount") in [0, None]):
                    issues.append(RiskIssue("불분명", process, "수량/단가/금액 누락", f"'{desc}' 항목의 단가 또는 금액 정보가 비어 있습니다.", "수량·단가·금액 3요소를 모두 기재해달라고 요청하세요."))

        return issues, detected_processes

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
