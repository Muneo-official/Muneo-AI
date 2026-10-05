"""리스크 진단 서비스 — 이미지 파싱 → 룰 기반 분석 + 가격 이상 탐지 → 리포트 조립.

종합프로젝트/risk_detector/service.py를 이관하되, 자체 파서(risk_detector/parser.py)와
청커(risk_detector/chunker.py) 대신 이미 검증된 pipeline 모듈(tool-use 파서, image_prep)을
그대로 쓴다. 룰 기반 분석(RiskAnalyzer)과 고층 양중비 컨텍스트 규칙은 원본 그대로 유지하고,
그 위에 코퍼스 기반 가격 이상 탐지(unit_price_reference — 품목의 단가 비교)를 추가한다.

Vision 파싱은 모든 이미지·청크를 동시에 호출한다. 처음엔 하나씩 기다려서 응답시간이 청크 수에
비례했다(S3 5청크 약 196초, S4 8청크 약 265초 — docs/RISK_DETECTOR_PERF_COST_LOG.md 베이스라인).

이미지 1장의 파싱 결과는 원본 바이트 해시로 캐시한다(RiskParseCacheRepository) — 같은 이미지를 다시
올리면 Vision을 다시 부르지 않고, 모델 출력의 실행 간 변동 없이 항상 같은 결과를 받는다.
"""

import asyncio
import hashlib
import time
from typing import Any

from starlette.concurrency import run_in_threadpool

from app.core.logging import log_event
from app.domain.risk_analyzer import RiskAnalyzer
from app.domain.risk_constants import SUPPORTED_SPACE_TYPES
from app.domain.risk_formatter import ResponseFormatter
from app.domain.risk_models import RiskIssue
from app.domain.unit_price_reference import UnitPriceReference
from app.repositories.risk_parse_cache_repository import CachedParse, RiskParseCacheRepository
from app.schemas.risk import AnalyzeRiskCommand
from pipeline.image_prep import prepare_chunks_from_bytes
from pipeline.parsing import merge_chunk_results
from pipeline.vision_client import (
    RISK_PARSE_VERSION,
    VisionCallResult,
    acall_vision_api_with_usage,
    get_async_client,
)

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


# 도구 미호출 시 재시도할 종료 사유 (_call_vision 참고)
_RETRYABLE_STOP_REASONS = {"end_turn"}


def _combine_calls(first: VisionCallResult, retry: VisionCallResult) -> VisionCallResult:
    """재시도한 호출의 결과를 쓰되, 시간·토큰은 두 호출을 합친다 — 비용 계측이 실제 과금과 맞도록."""
    return VisionCallResult(
        result=retry.result,
        latency_s=first.latency_s + retry.latency_s,
        input_tokens=first.input_tokens + retry.input_tokens,
        output_tokens=first.output_tokens + retry.output_tokens,
        cache_creation_input_tokens=first.cache_creation_input_tokens + retry.cache_creation_input_tokens,
        cache_read_input_tokens=first.cache_read_input_tokens + retry.cache_read_input_tokens,
        tool_called=retry.tool_called,
        stop_reason=retry.stop_reason,
        model=retry.model,
    )


class RiskDetectorService:
    def __init__(
        self,
        vision_max_concurrency: int = 20,
        vision_max_concurrency_per_request: int = 8,
        parse_cache: RiskParseCacheRepository | None = None,
        unit_prices: UnitPriceReference | None = None,
    ) -> None:
        # None이면 단가 지적을 하지 않는다 (기준표가 아직 없을 때)
        self.unit_prices = unit_prices
        # None이면 캐시 없이 매번 Vision을 호출한다 (설정으로 끌 때, 벤치에서 반복 측정할 때)
        self._parse_cache = parse_cache
        self.analyzer = RiskAnalyzer()
        self.formatter = ResponseFormatter()
        # 서비스는 앱당 하나(app.state)라 이 세마포어가 곧 프로세스 전체의 동시 Vision 호출 상한이다.
        # 요청 단위로만 제한하면 동시 요청 수 × 청크 수만큼 Anthropic에 몰려 조직 rate limit을 넘을 수 있다.
        self._vision_slots = asyncio.Semaphore(vision_max_concurrency)
        # 요청 하나가 전역 슬롯을 독점하지 않게 하는 요청당 상한. 업로드 이미지 수에 제한이 없어서,
        # 이게 없으면 큰 요청 하나가 슬롯을 다 차지하고 그동안 다른 사용자가 전부 기다린다.
        self._per_request_limit = vision_max_concurrency_per_request

    async def analyze(self, command: AnalyzeRiskCommand) -> dict[str, Any]:
        self._validate_input(command)
        started = time.perf_counter()

        all_items, vision_calls, cache_log = await self._parse_images(command.image_files)
        parsed_at = time.perf_counter()
        rule_analyze_s = price_check_s = 0.0

        if all_items:
            issues, detected_processes = self.analyzer.analyze(all_items)
            self._add_contextual_issues(command, all_items, issues, detected_processes)
            rule_done_at = time.perf_counter()
            rule_analyze_s = rule_done_at - parsed_at

            price_issues = (
                self.unit_prices.issues(all_items) + self.unit_prices.quantity_issues(all_items, command.pyeong)
                if self.unit_prices else []
            )
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
            **cache_log,
        )
        return result

    async def _parse_images(
        self, image_files: list[bytes]
    ) -> tuple[list[dict[str, Any]], list[VisionCallResult], dict[str, Any]]:
        """이미지별 파싱 결과를 (이미지 순서대로) 합친 항목, 실제 Vision 호출 목록, 캐시 로그 필드를 반환한다."""
        started = time.perf_counter()
        digests = [hashlib.sha256(raw).hexdigest() for raw in image_files]
        keys = [f"{digest}:{RISK_PARSE_VERSION}" for digest in digests]
        cached = await self._cache_get(keys)
        # 캐시가 응답시간에 더하는 비용(해시 + Mongo 조회) — 미스일 때 이만큼이 순수 오버헤드다
        lookup_s = time.perf_counter() - started

        # 캐시에 없는 이미지만 파싱한다. 같은 요청에 같은 이미지가 여러 장 있으면 한 번만.
        to_parse: dict[str, tuple[int, bytes]] = {}
        for image_index, (key, raw) in enumerate(zip(keys, image_files)):
            if key not in cached and key not in to_parse:
                to_parse[key] = (image_index, raw)
        parsed, calls = await self._parse_uncached(to_parse)

        # 빈 결과는 저장하지 않는다 — 일시적인 파싱 실패가 "추출 실패"로 굳지 않도록.
        # 청크 중 하나라도 재시도 후에도 도구를 안 불렀으면(거부·출력 한도 포함) 그 이미지도 저장하지 않는다 — 나머지
        # 청크 항목만으로 결과가 비어 있지 않아도, 빠진 청크가 있는 불완전한 결과가 재업로드마다 고정돼 나가게 된다.
        # 이미지가 여러 장이면 저장을 동시에 보내 Mongo 왕복이 이미지 수만큼 쌓이지 않게 한다.
        put_started = time.perf_counter()
        await asyncio.gather(*(
            self._cache_put(key, CachedParse(
                line_items=line_items,
                input_tokens=sum(c.input_tokens for c in image_calls),
                output_tokens=sum(c.output_tokens for c in image_calls),
            ))
            for key, (line_items, image_calls) in parsed.items()
            if line_items and all(c.tool_called for c in image_calls)
        ))
        store_s = time.perf_counter() - put_started

        all_items: list[dict[str, Any]] = []
        for key in keys:
            line_items = cached[key].line_items if key in cached else parsed[key][0]
            all_items.extend(self._merge_across_images(all_items, line_items))

        hits = [key for key in keys if key in cached]
        cache_log = {
            "image_sha256": digests,  # 캐시를 꺼도 남긴다 — 재업로드 비율을 로그로 셀 수 있게
            "parse_cache_hits": len(hits),
            "parse_cache_misses": len(keys) - len(hits),
            "parse_cache_saved_input_tokens": sum(cached[key].input_tokens for key in hits),
            "parse_cache_saved_output_tokens": sum(cached[key].output_tokens for key in hits),
            "parse_cache_lookup_s": round(lookup_s, 4),
            "parse_cache_store_s": round(store_s, 4),
        }
        return all_items, calls, cache_log

    async def _cache_get(self, keys: list[str]) -> dict[str, CachedParse]:
        if self._parse_cache is None:
            return {}
        try:
            return await self._parse_cache.get_many(keys)
        except Exception as e:
            # 캐시 장애로 분석 자체가 실패하면 안 된다 — 캐시 없이 Vision을 호출하는 원래 경로로
            log_event("risk_parse_cache_error", op="get", error=repr(e))
            return {}

    async def _cache_put(self, key: str, parsed: CachedParse) -> None:
        if self._parse_cache is None:
            return
        try:
            await self._parse_cache.put(key, parsed)
        except Exception as e:
            log_event("risk_parse_cache_error", op="put", error=repr(e))

    async def _parse_uncached(
        self, to_parse: dict[str, tuple[int, bytes]]
    ) -> tuple[dict[str, tuple[list[dict[str, Any]], list[VisionCallResult]]], list[VisionCallResult]]:
        """캐시 키별 (병합된 line_items, 그 이미지의 Vision 호출들)과 전체 호출 목록을 반환한다."""
        if not to_parse:
            return {}, []
        client = get_async_client()
        # 리사이즈·PNG 인코딩은 CPU 작업이라 이벤트 루프를 막지 않게 threadpool에서
        chunks_per_image = [
            (image_index, await run_in_threadpool(prepare_chunks_from_bytes, raw))
            for image_index, raw in to_parse.values()
        ]

        request_slots = asyncio.Semaphore(self._per_request_limit)
        tasks = [
            asyncio.create_task(
                self._call_vision(client, request_slots, image_index, chunk_index, len(chunks), chunk)
            )
            for image_index, chunks in chunks_per_image
            for chunk_index, chunk in enumerate(chunks)
        ]
        try:
            calls = await asyncio.gather(*tasks)
        except BaseException:
            # 하나가 실패하면 나머지 호출도 취소한다 — 어차피 버릴 결과에 API 비용을 쓰지 않도록.
            # (gather는 첫 예외만 올리고 나머지 태스크는 그대로 돌려둔다)
            for task in tasks:
                task.cancel()
            # cancel()은 취소를 예약만 할 뿐 실제로 끝나길 기다리지 않는다. 여기서 마저 기다려
            # asyncio가 "Task exception was never retrieved" 경고를 내지 않도록 결과를 모두 수거한다.
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        # 완료 순서와 무관하게 (이미지, 청크) 순서로 다시 모아 이미지 단위로 청크 병합한다
        # (이미지 간 병합은 호출자가 캐시 결과와 섞어 원래 업로드 순서대로 한다)
        parsed: dict[str, tuple[list[dict[str, Any]], list[VisionCallResult]]] = {}
        cursor = 0
        for key, (_, chunks) in zip(to_parse, chunks_per_image):
            image_calls = list(calls[cursor : cursor + len(chunks)])
            cursor += len(chunks)
            merged = merge_chunk_results([call.result for call in image_calls], for_risk=True)
            parsed[key] = (merged.get("line_items", []), image_calls)
        return parsed, list(calls)

    async def _call_vision(
        self,
        client,
        request_slots: asyncio.Semaphore,
        image_index: int,
        chunk_index: int,
        chunk_count: int,
        chunk: bytes,
    ) -> VisionCallResult:
        queued_at = time.perf_counter()
        # 요청당 → 전역 순서로 잡는다. 반대로 잡으면 요청당 상한에 막힌 호출이 전역 슬롯을 쥔 채 기다린다.
        async with request_slots, self._vision_slots:
            wait_s = time.perf_counter() - queued_at
            call = await acall_vision_api_with_usage(chunk, client)
            retried = False
            if not call.tool_called and call.stop_reason in _RETRYABLE_STOP_REASONS:
                # 강제 도구 호출이 안 되는 모델(auto)은 드물게 도구 대신 글로 답할 수 있다 — 그대로 두면 그 청크가
                # "견적서 아님"으로 버려진다. 한 번만 다시 부른다. 정상 종료(end_turn)만 재시도한다 — 거부(refusal)는
                # 다시 보내도 또 거부되고, 출력 한도(max_tokens)로 잘린 건 같은 요청이면 또 잘려 비용만 두 배가 된다.
                retry = await acall_vision_api_with_usage(chunk, client)
                call = _combine_calls(call, retry)
                retried = True
        log_event(
            "risk_vision_call",
            image_index=image_index,
            chunk_index=chunk_index,
            chunk_count=chunk_count,
            latency_s=round(call.latency_s, 3),
            slot_wait_s=round(wait_s, 3),  # 상한(요청당·전역)에 막혀 기다린 시간 — 부하 테스트에서 상한이 병목인지 본다
            input_tokens=call.input_tokens,
            output_tokens=call.output_tokens,
            cache_creation_input_tokens=call.cache_creation_input_tokens,
            cache_read_input_tokens=call.cache_read_input_tokens,
            tool_called=call.tool_called,
            retried=retried,
            stop_reason=call.stop_reason,
            served_model=call.model,
        )
        return call

    def _merge_across_images(
        self, already_collected: list[dict[str, Any]], new_items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """여러 장의 이미지(같은 견적서의 여러 페이지)에서 나온 항목을 합치며, 앞 이미지에 이미 있던 줄은 뺀다.

        pipeline.parsing.merge_chunk_results()는 한 이미지 내 청크 병합용이라 이미지 간
        병합엔 안 맞는다(원본 risk_detector/service.py의 자체 dedup 로직 그대로 이관).

        같은 이미지 안에서 똑같은 줄이 두 번 나오면 둘 다 남긴다. 견적서에 같은 줄이 두 번 적힌 것이고, 그것이
        중복 지적의 대상이다 — 예전에는 여기서 한 줄을 지워서 중복 규칙이 볼 때는 이미 한 줄뿐이었다.
        """
        seen = {
            (i.get("category", ""), i.get("description", ""), int(i.get("amount") or 0))
            for i in already_collected
        }
        return [
            item for item in new_items
            if (item.get("category", ""), item.get("description", ""), int(item.get("amount") or 0)) not in seen
        ]

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
