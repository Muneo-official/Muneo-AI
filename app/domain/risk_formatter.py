from collections import Counter, defaultdict
from datetime import date
from typing import Any

from app.domain.risk_constants import PROCESS_CATEGORY_MAP, PROCESS_DISPLAY_NAME
from app.domain.risk_models import RiskIssue


class ResponseFormatter:
    def build(self, *, company_name: str, space_type: str, pyeong: int, room_count: int, floor: int, elevator: bool, region: str, building_age: str, line_items: list[dict[str, Any]], issues: list[RiskIssue], requested_processes: list[str]) -> dict[str, Any]:
        process_groups = self._build_process_groups(line_items, issues, requested_processes)
        counts = Counter(i.type for i in issues)
        return {
            "report": {
                "title": "진단 레포트",
                "subtitle_fields": {"company_name": company_name, "pyeong": pyeong, "analyzed_date": date.today().isoformat()},
                "construction_info": {"space_type": space_type, "room_count": room_count, "floor": floor, "elevator": elevator, "region": region, "building_age": building_age},
                "cards": {
                    "missing": {"label": "누락항목", "count": counts.get("누락", 0), "description": "필수 항목 미포함"},
                    "duplicate": {"label": "중복항목", "count": counts.get("중복", 0), "description": "동일 항목 반복 기재"},
                    "unclear": {"label": "불분명", "count": counts.get("불분명", 0), "description": "모호 표현/정보 불충분"},
                },
                "process_sections": process_groups,
                "summary": {
                    "title": "진단 요약",
                    "total_risk_items": len(issues),
                    "chips": {"누락": counts.get("누락", 0), "중복": counts.get("중복", 0), "불분명": counts.get("불분명", 0)},
                },
            }
        }

    def _build_process_groups(self, line_items: list[dict[str, Any]], issues: list[RiskIssue], requested_processes: list[str]) -> list[dict[str, Any]]:
        grouped_items = defaultdict(list)
        for item in line_items:
            process = self._process_from_category(item.get("category", ""))
            if process:
                grouped_items[process].append(item)

        issue_map = defaultdict(list)
        for issue in issues:
            issue_map[issue.process].append(issue)

        sections = []
        for process in requested_processes:
            cards = []
            for item in grouped_items.get(process, []):
                cards.append({"status": "정상", "title": item.get("description", "항목"), "description": f"{int(item.get('amount') or 0):,}원 · {item.get('notes', '')}".strip(" ·"), "guide": ""})
            for issue in issue_map.get(process, []):
                cards.append({"status": issue.type, "title": issue.title, "description": issue.detail, "guide": issue.guide})
            sections.append({"process": process, "display_name": PROCESS_DISPLAY_NAME.get(process, process), "items": cards})
        return sections

    def _process_from_category(self, category: str) -> str | None:
        for process, categories in PROCESS_CATEGORY_MAP.items():
            if category in categories:
                return process
        return None
