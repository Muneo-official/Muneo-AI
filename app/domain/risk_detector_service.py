"""리스크 진단 서비스 — 이미지 파싱 → 룰 기반 분석 + 가격 이상 탐지 → 리포트 조립.

종합프로젝트/risk_detector/service.py를 이관하되, 자체 파서(risk_detector/parser.py)와
청커(risk_detector/chunker.py) 대신 이미 검증된 pipeline 모듈(tool-use 파서, image_prep)을
그대로 쓴다. 룰 기반 분석(RiskAnalyzer)과 고층 양중비 컨텍스트 규칙은 원본 그대로 유지하고,
그 위에 코퍼스 기반 가격 이상 탐지(risk_price_checker)를 추가한다.
"""

import time
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.core.logging import log_event
from app.domain.estimate_engine import EstimateEngine
from app.domain.risk_analyzer import RiskAnalyzer
from app.domain.risk_constants import SUPPORTED_SPACE_TYPES
from app.domain.risk_formatter import ResponseFormatter
from app.domain.risk_models import RiskIssue
from app.domain.risk_price_checker import check_price_anomalies
from app.schemas.risk import AnalyzeRiskCommand
from pipeline.image_prep import prepare_chunks_from_bytes
from pipeline.parsing import merge_chunk_results
from pipeline.vision_client import VisionCallResult, call_vision_api_with_usage, get_client

CONTEXT_CARRYING_KEYWORDS = [
    "양중",
    "운반",
    "사다리차",
    "엘리베이터",
    "EV",
    "계단",
    "양중비",
    "운반비",
    "하역",
]


class RiskDetectorService:
    def __init__(self, engine: EstimateEngine) -> None:
        self._engine = engine
        self.analyzer = RiskAnalyzer()
        self.formatter = ResponseFormatter()

    async def analyze(self, command: AnalyzeRiskCommand) -> dict[str, Any]:
        self._validate_input(command)
        started = time.perf_counter()

        all_items, vision_calls = await self._parse_images(command.image_files)
        parsed_at = time.perf_counter()
        rule_analyze_s = price_check_s = 0.0

        if all_items:
            issues, detected_processes = self.analyzer.analyze(all_items)
            self._add_contextual_issues(command, all_items, issues, detected_processes)
            rule_done_at = time.perf_counter()
            rule_analyze_s = rule_done_at - parsed_at

            price_issues = await check_price_anomalies(command, all_items, self._engine)
            price_check_s = time.perf_counter() - rule_done_at
            issues.extend(price_issues)
            for issue in price_issues:
                if issue.process not in detected_processes:
                    detected_processes.append(issue.process)
        else:
            issues = [
                RiskIssue(
                    "불분명",
                    "견적서",
                    "견적서 항목 추출 실패",
                    "업로드한 견적서에서 분석 가능한 품목을 추출하지 못했습니다.",
                    "이미지 해상도, 파일 형식, 견적서 표 영역이 선명한지 확인한 뒤 다시 업로드하세요.",
                )
            ]
            detected_processes = ["견적서"]

        result = self.formatter.build(
            company_name=command.company_name,
            space_type=command.space_type,
            pyeong=command.pyeong,
            room_count=command.room_count,
            floor=command.floor,
            elevator=command.elevator,
            region=command.region,
            building_age=command.building_age,
            line_items=all_items,
            issues=issues,
            requested_processes=detected_processes,
        )

        # 응답시간·비용 베이스라인 계측 (docs/RISK_DETECTOR_PERF_COST_LOG.md)
        log_event(
            "risk_analyze_timing",
            image_count=len(command.image_files),
            chunk_count=len(vision_calls),
            line_item_count=len(all_items),
            parse_images_s=round(parsed_at - started, 3),
            vision_latency_sum_s=round(sum(c.latency_s for c in vision_calls), 3),
            rule_analyze_s=round(rule_analyze_s, 3),
            price_check_s=round(price_check_s, 3),
            total_s=round(time.perf_counter() - started, 3),
            input_tokens=sum(c.input_tokens for c in vision_calls),
            output_tokens=sum(c.output_tokens for c in vision_calls),
            cache_creation_input_tokens=sum(c.cache_creation_input_tokens for c in vision_calls),
            cache_read_input_tokens=sum(c.cache_read_input_tokens for c in vision_calls),
        )
        return result

    async def _parse_images(
        self, image_files: list[bytes]
    ) -> tuple[list[dict[str, Any]], list[VisionCallResult]]:
        client = get_client()
        all_items: list[dict[str, Any]] = []
        vision_calls: list[VisionCallResult] = []
        for image_index, raw in enumerate(image_files):
            chunks = prepare_chunks_from_bytes(raw)
            chunk_results = []
            for chunk_index, chunk in enumerate(chunks):
                call = await run_in_threadpool(call_vision_api_with_usage, chunk, client)
                log_event(
                    "risk_vision_call",
                    image_index=image_index,
                    chunk_index=chunk_index,
                    chunk_count=len(chunks),
                    latency_s=round(call.latency_s, 3),
                    input_tokens=call.input_tokens,
                    output_tokens=call.output_tokens,
                    cache_creation_input_tokens=call.cache_creation_input_tokens,
                    cache_read_input_tokens=call.cache_read_input_tokens,
                )
                vision_calls.append(call)
                chunk_results.append(call.result)
            merged = merge_chunk_results(chunk_results)
            all_items.extend(self._merge_across_images(all_items, merged.get("line_items", [])))
        return all_items, vision_calls

    def _merge_across_images(
        self, already_collected: list[dict[str, Any]], new_items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """여러 장의 이미지(같은 견적서의 여러 페이지)에서 나온 항목을 합치며 중복 제거.

        pipeline.parsing.merge_chunk_results()는 한 이미지 내 청크 병합용이라 이미지 간
        병합엔 안 맞는다(원본 risk_detector/service.py의 자체 dedup 로직 그대로 이관).
        """
        seen = {
            (i.get("category", ""), i.get("description", ""), int(i.get("amount") or 0))
            for i in already_collected
        }
        merged = []
        for item in new_items:
            key = (item.get("category", ""), item.get("description", ""), int(item.get("amount") or 0))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)
        return merged

    def _validate_input(self, command: AnalyzeRiskCommand) -> None:
        if command.space_type not in SUPPORTED_SPACE_TYPES:
            raise ValueError(f"지원하지 않는 공간유형입니다: {command.space_type}")
        if not command.image_files or not any(command.image_files):
            raise ValueError("최소 1개 이상의 견적서 이미지가 필요합니다.")

    def _add_contextual_issues(
        self,
        command: AnalyzeRiskCommand,
        line_items: list[dict[str, Any]],
        issues: list[RiskIssue],
        detected_processes: list[str],
    ) -> None:
        if command.floor < 5:
            return

        search_text = " ".join(
            f"{item.get('category', '')} {item.get('description', '')} {item.get('notes', '')}"
            for item in line_items
        )
        has_carrying_cost = any(keyword in search_text for keyword in CONTEXT_CARRYING_KEYWORDS)
        if has_carrying_cost:
            return

        issues.append(
            RiskIssue(
                "불분명",
                "공통",
                "고층 시공 운반/양중 비용 정보 미기재",
                f"{command.floor}층 시공 조건이지만 견적서에서 양중/운반 관련 항목이 확인되지 않습니다.",
                "고층 작업 시 운반비·양중비·사다리차 비용 포함 여부를 업체에 확인하세요.",
            )
        )
        if "공통" not in detected_processes:
            detected_processes.append("공통")
