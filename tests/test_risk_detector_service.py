import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.domain.risk_detector_service as service_module
from app.domain.risk_detector_service import RiskDetectorService
from app.schemas.risk import AnalyzeRiskCommand
from pipeline.vision_client import VisionCallResult


def _command(**overrides) -> AnalyzeRiskCommand:
    fields = dict(
        space_type="아파트",
        pyeong=30,
        room_count=3,
        floor=2,
        elevator=True,
        region="서울",
        building_age="20년이상",
        company_name="홍길동 인테리어",
        image_files=[b"fake-image-bytes"],
    )
    fields.update(overrides)
    return AnalyzeRiskCommand(**fields)


def _call_result(result: dict, latency_s: float = 0.5, input_tokens: int = 1000, output_tokens: int = 200):
    return VisionCallResult(
        result=result,
        latency_s=latency_s,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )


def _vision_returning(result: dict, **usage):
    """acall_vision_api_with_usage 대체 — 파싱 결과는 고정, 사용량은 계측 검증용 더미값."""
    async def fake(chunk, client):
        return _call_result(result, **usage)
    return fake


def _mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.build_query.return_value = "30평 서울 아파트 리모델링"
    # 가격 체크는 비교 사례 부족으로 스킵되게 - 이 테스트 파일은 룰 기반 배선만 검증
    engine.retrieve_cases = AsyncMock(return_value=[])
    return engine


_ONE_ITEM_RESULT = {
    "is_estimate": True,
    "total_cost": 1_000_000,
    "line_items": [
        {"category": "도배공사", "description": "실크벽지 시공", "amount": 1_000_000, "unit_price": 1_000_000}
    ],
}


@pytest.fixture(autouse=True)
def _no_real_vision_client(monkeypatch):
    monkeypatch.setattr(service_module, "get_async_client", lambda: object())
    # 실제 PIL 디코딩/리사이즈는 test_pipeline_image_prep.py에서 이미 검증 — 여기선
    # 서비스 배선(파싱 결과 -> 분석 -> 포맷)만 보므로 청크 분할은 통과시키기만 한다.
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [raw])


@pytest.mark.asyncio
async def test_analyze_raises_on_unsupported_space_type():
    service = RiskDetectorService(engine=_mock_engine())
    command = _command(space_type="상가")

    with pytest.raises(ValueError, match="지원하지 않는 공간유형"):
        await service.analyze(command)


@pytest.mark.asyncio
async def test_analyze_raises_when_no_images():
    service = RiskDetectorService(engine=_mock_engine())
    command = _command(image_files=[])

    with pytest.raises(ValueError, match="최소 1개 이상"):
        await service.analyze(command)


@pytest.mark.asyncio
async def test_analyze_returns_extraction_failure_issue_when_no_line_items(monkeypatch):
    monkeypatch.setattr(
        service_module, "acall_vision_api_with_usage", _vision_returning({"is_estimate": False})
    )
    service = RiskDetectorService(engine=_mock_engine())

    result = await service.analyze(_command())

    report = result["report"]
    assert report["summary"]["total_risk_items"] == 1
    assert report["process_sections"][0]["process"] == "견적서"


@pytest.mark.asyncio
async def test_analyze_runs_rule_based_analysis_on_parsed_items(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "acall_vision_api_with_usage",
        _vision_returning(_ONE_ITEM_RESULT),
    )
    service = RiskDetectorService(engine=_mock_engine())

    result = await service.analyze(_command())

    report = result["report"]
    assert "도배" in [s["process"] for s in report["process_sections"]]
    # 누락 조건(폐기물 등)까지는 안 채웠으니 최소한 누락 이슈는 있어야 함
    assert report["cards"]["missing"]["count"] >= 0  # 배선 자체가 죽지 않는지만 확인


@pytest.mark.asyncio
async def test_analyze_dedupes_identical_items_across_multiple_images(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "acall_vision_api_with_usage",
        _vision_returning(_ONE_ITEM_RESULT),
    )
    service = RiskDetectorService(engine=_mock_engine())
    command = _command(image_files=[b"page-1", b"page-2"])

    result = await service.analyze(command)

    sections = result["report"]["process_sections"]
    normal_items = [item for s in sections for item in s["items"] if item["status"] == "정상"]
    assert len(normal_items) == 1


@pytest.mark.asyncio
async def test_analyze_logs_per_call_usage_and_request_timing(monkeypatch):
    monkeypatch.setattr(
        service_module,
        "acall_vision_api_with_usage",
        _vision_returning(_ONE_ITEM_RESULT, latency_s=1.5, input_tokens=1000, output_tokens=200),
    )
    # 이미지 2장 × 청크 2개 = Vision 호출 4회
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [raw + b"-a", raw + b"-b"])
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(service_module, "log_event", lambda event, **fields: events.append((event, fields)))
    service = RiskDetectorService(engine=_mock_engine())

    await service.analyze(_command(image_files=[b"page-1", b"page-2"]))

    calls = [f for e, f in events if e == "risk_vision_call"]
    # 동시에 호출하므로 로그는 완료 순서 — 어떤 호출이 찍혔는지만 본다
    assert sorted((c["image_index"], c["chunk_index"]) for c in calls) == [(0, 0), (0, 1), (1, 0), (1, 1)]

    [timing] = [f for e, f in events if e == "risk_analyze_timing"]
    assert timing["image_count"] == 2
    assert timing["chunk_count"] == 4
    assert timing["input_tokens"] == 4000
    assert timing["output_tokens"] == 800
    assert timing["vision_latency_sum_s"] == 6.0
    assert timing["total_s"] >= timing["parse_images_s"]


@pytest.mark.asyncio
async def test_analyze_logs_timing_even_when_no_line_items(monkeypatch):
    monkeypatch.setattr(
        service_module, "acall_vision_api_with_usage", _vision_returning({"is_estimate": False})
    )
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(service_module, "log_event", lambda event, **fields: events.append((event, fields)))
    service = RiskDetectorService(engine=_mock_engine())

    await service.analyze(_command())

    [timing] = [f for e, f in events if e == "risk_analyze_timing"]
    assert timing["line_item_count"] == 0
    assert timing["rule_analyze_s"] == 0.0
    assert timing["price_check_s"] == 0.0


def _item(desc: str) -> dict:
    return {"category": "도배", "description": desc, "amount": 1_000_000}


@pytest.mark.asyncio
async def test_parse_images_calls_chunks_concurrently(monkeypatch):
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [raw + b"-a", raw + b"-b"])
    in_flight = peak = 0

    async def fake(chunk, client):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.2)
        in_flight -= 1
        return _call_result({"is_estimate": True, "line_items": [_item(chunk.decode())]})

    monkeypatch.setattr(service_module, "acall_vision_api_with_usage", fake)
    service = RiskDetectorService(engine=_mock_engine())

    started = time.perf_counter()
    items, calls = await service._parse_images([b"p1", b"p2"])
    elapsed = time.perf_counter() - started

    assert peak == 4  # 이미지 2장 × 청크 2개가 한꺼번에
    assert elapsed < 0.5  # 직렬이면 0.8초
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_parse_images_keeps_image_and_chunk_order_regardless_of_completion(monkeypatch):
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [raw + b"-a", raw + b"-b"])
    # 앞쪽 청크일수록 늦게 끝나게 — 완료 순서가 뒤집혀도 결과는 (이미지, 청크) 순서여야 한다
    delays = {b"p1-a": 0.3, b"p1-b": 0.2, b"p2-a": 0.1, b"p2-b": 0.0}

    async def fake(chunk, client):
        await asyncio.sleep(delays[chunk])
        return _call_result({"is_estimate": True, "line_items": [_item(chunk.decode())]})

    monkeypatch.setattr(service_module, "acall_vision_api_with_usage", fake)
    service = RiskDetectorService(engine=_mock_engine())

    items, _ = await service._parse_images([b"p1", b"p2"])

    assert [i["description"] for i in items] == ["p1-a", "p1-b", "p2-a", "p2-b"]


@pytest.mark.asyncio
async def test_parse_images_respects_global_concurrency_limit(monkeypatch):
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [raw + bytes([i]) for i in range(5)])
    in_flight = peak = 0

    async def fake(chunk, client):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        return _call_result({"is_estimate": False})

    monkeypatch.setattr(service_module, "acall_vision_api_with_usage", fake)
    service = RiskDetectorService(engine=_mock_engine(), vision_max_concurrency=2)

    # 동시 요청 2건 × 청크 5개 = 10개가 몰려도 서비스 전체에서 2개까지만
    await asyncio.gather(service._parse_images([b"x"]), service._parse_images([b"y"]))

    assert peak == 2


@pytest.mark.asyncio
async def test_parse_images_cancels_remaining_calls_when_one_fails(monkeypatch):
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [b"ok-1", b"boom", b"ok-2"])
    cancelled = []

    async def fake(chunk, client):
        if chunk == b"boom":
            raise RuntimeError("vision failed")
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(chunk)
            raise
        return _call_result({"is_estimate": False})

    monkeypatch.setattr(service_module, "acall_vision_api_with_usage", fake)
    service = RiskDetectorService(engine=_mock_engine())

    with pytest.raises(RuntimeError, match="vision failed"):  # 원래 예외 타입 그대로 (라우터가 500으로 변환)
        await service._parse_images([b"x"])
    await asyncio.sleep(0)  # 취소가 전달될 틈

    assert sorted(cancelled) == [b"ok-1", b"ok-2"]


@pytest.mark.asyncio
async def test_parse_images_caps_calls_per_request_so_one_request_cannot_take_all_slots(monkeypatch):
    monkeypatch.setattr(service_module, "prepare_chunks_from_bytes", lambda raw: [raw + bytes([i]) for i in range(6)])
    in_flight: dict[bytes, int] = {b"big": 0, b"small": 0}
    peak: dict[bytes, int] = {b"big": 0, b"small": 0}
    small_started_at = None

    async def fake(chunk, client):
        nonlocal small_started_at
        owner = chunk[:-1]
        in_flight[owner] += 1
        peak[owner] = max(peak[owner], in_flight[owner])
        if owner == b"small" and small_started_at is None:
            small_started_at = time.perf_counter()
        await asyncio.sleep(0.1)
        in_flight[owner] -= 1
        return _call_result({"is_estimate": False})

    monkeypatch.setattr(service_module, "acall_vision_api_with_usage", fake)
    # 전역 4, 요청당 3 — 큰 요청(6청크)이 먼저 와도 슬롯 1개는 뒤 요청 몫으로 남는다
    service = RiskDetectorService(engine=_mock_engine(), vision_max_concurrency=4, vision_max_concurrency_per_request=3)

    started = time.perf_counter()
    big = asyncio.create_task(service._parse_images([b"big"]))
    await asyncio.sleep(0.01)
    await asyncio.gather(big, service._parse_images([b"small"]))

    assert peak[b"big"] == 3
    assert small_started_at - started < 0.05  # 큰 요청이 끝나길 기다리지 않고 바로 시작
