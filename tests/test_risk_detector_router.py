"""
라우터 계층 테스트 — get_risk_detector_service/get_risk_report_repository를 mock으로
오버라이드하므로 실제 Vision API/Mongo 호출은 트리거되지 않는다. 서비스 자체의 정확도는
tests/test_risk_detector_service.py에서 다룬다.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.deps import get_risk_detector_service, get_risk_report_repository
from app.main import app

FORM_DATA = {
    "space_type": "아파트",
    "pyeong": "30",
    "room_count": "3",
    "floor": "8",
    "elevator": "true",
    "region": "서울",
    "building_age": "20년이상",
    "company_name": "홍길동 인테리어",
}
FILES = [("files", ("estimate.png", b"fake-png-bytes", "image/png"))]
USER_HEADER = {"x-user-id": "user_abc"}

SAMPLE_REPORT = {
    "report": {
        "title": "진단 레포트",
        "subtitle_fields": {"company_name": "홍길동 인테리어", "pyeong": 30, "analyzed_date": "2026-09-05"},
        "construction_info": {
            "space_type": "아파트", "room_count": 3, "floor": 8,
            "elevator": True, "region": "서울", "building_age": "20년이상",
        },
        "cards": {
            "missing": {"label": "누락항목", "count": 0, "description": "필수 항목 미포함"},
            "duplicate": {"label": "중복항목", "count": 0, "description": "동일 항목 반복 기재"},
            "unclear": {"label": "불분명", "count": 0, "description": "모호 표현/정보 불충분"},
        },
        "process_sections": [],
        "summary": {"title": "진단 요약", "total_risk_items": 0, "chips": {"누락": 0, "중복": 0, "불분명": 0}},
    }
}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_analyze_risk_success(client: AsyncClient):
    mock_service = AsyncMock()
    mock_service.analyze.return_value = SAMPLE_REPORT
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service

    resp = await client.post("/risk-detector/analyze", data=FORM_DATA, files=FILES)

    assert resp.status_code == 200
    assert resp.json() == SAMPLE_REPORT
    mock_service.analyze.assert_awaited_once()


async def test_analyze_risk_invalid_input_returns_422(client: AsyncClient):
    mock_service = AsyncMock()
    mock_service.analyze.side_effect = ValueError("지원하지 않는 공간유형입니다: 상가")
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service

    resp = await client.post("/risk-detector/analyze", data={**FORM_DATA, "space_type": "상가"}, files=FILES)

    assert resp.status_code == 422
    assert "지원하지 않는 공간유형" in resp.json()["detail"]


async def test_analyze_risk_unexpected_error_returns_500(client: AsyncClient):
    mock_service = AsyncMock()
    mock_service.analyze.side_effect = RuntimeError("vision api down")
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service

    resp = await client.post("/risk-detector/analyze", data=FORM_DATA, files=FILES)

    assert resp.status_code == 500


async def test_analyze_risk_rate_limited_after_5_per_minute(client: AsyncClient):
    mock_service = AsyncMock()
    mock_service.analyze.return_value = SAMPLE_REPORT
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service

    statuses = []
    for _ in range(6):
        resp = await client.post(
            "/risk-detector/analyze", data=FORM_DATA, files=FILES,
            headers={"x-user-id": "risk_rate_limit_test_user"},
        )
        statuses.append(resp.status_code)

    assert statuses[:5] == [200] * 5
    assert statuses[5] == 429


async def test_save_report(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.save.return_value = "report_id_123"
    app.dependency_overrides[get_risk_report_repository] = lambda: mock_repo

    resp = await client.post(
        "/risk-detector/save",
        json={"input": {"pyeong": 30}, "result": SAMPLE_REPORT},
        headers=USER_HEADER,
    )

    assert resp.status_code == 201
    assert resp.json() == {"id": "report_id_123"}


async def test_save_report_missing_fields_returns_400(client: AsyncClient):
    mock_repo = AsyncMock()
    app.dependency_overrides[get_risk_report_repository] = lambda: mock_repo

    resp = await client.post("/risk-detector/save", json={"input": {}}, headers=USER_HEADER)

    assert resp.status_code == 400
    mock_repo.save.assert_not_awaited()


async def test_list_reports(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.list_by_user.return_value = [{"id": "1"}, {"id": "2"}]
    app.dependency_overrides[get_risk_report_repository] = lambda: mock_repo

    resp = await client.get("/risk-detector", headers=USER_HEADER)

    assert resp.status_code == 200
    assert resp.json() == [{"id": "1"}, {"id": "2"}]


async def test_delete_report_not_found_returns_404(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.delete.return_value = False
    app.dependency_overrides[get_risk_report_repository] = lambda: mock_repo

    resp = await client.delete("/risk-detector/507f1f77bcf86cd799439011", headers=USER_HEADER)

    assert resp.status_code == 404


async def test_delete_report_invalid_id_returns_400(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.delete.return_value = None
    app.dependency_overrides[get_risk_report_repository] = lambda: mock_repo

    resp = await client.delete("/risk-detector/not-a-valid-object-id", headers=USER_HEADER)

    assert resp.status_code == 400


async def test_analyze_risk_too_many_files_returns_422_without_calling_service(client: AsyncClient):
    mock_service = AsyncMock()
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service
    files = [("files", (f"{i}.png", b"fake-png-bytes", "image/png")) for i in range(11)]

    resp = await client.post(
        "/risk-detector/analyze", data=FORM_DATA, files=files, headers={"x-user-id": "risk_guard_count_user"},
    )

    assert resp.status_code == 422
    assert "10장" in resp.json()["detail"]
    mock_service.analyze.assert_not_awaited()


async def test_analyze_risk_empty_file_returns_422_without_calling_service(client: AsyncClient):
    mock_service = AsyncMock()
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service
    files = [*FILES, ("files", ("empty.png", b"", "image/png"))]

    resp = await client.post(
        "/risk-detector/analyze", data=FORM_DATA, files=files, headers={"x-user-id": "risk_guard_empty_user"},
    )

    assert resp.status_code == 422
    mock_service.analyze.assert_not_awaited()


async def test_analyze_risk_500_does_not_leak_exception_text(client: AsyncClient):
    mock_service = AsyncMock()
    mock_service.analyze.side_effect = RuntimeError("cannot identify image file <_io.BytesIO object at 0x0001>")
    app.dependency_overrides[get_risk_detector_service] = lambda: mock_service

    resp = await client.post(
        "/risk-detector/analyze", data=FORM_DATA, files=FILES, headers={"x-user-id": "risk_guard_500_user"},
    )

    assert resp.status_code == 500
    assert "BytesIO" not in resp.text and "cannot identify" not in resp.text


async def test_analyze_risk_real_service_rejects_non_image_before_vision(client: AsyncClient, monkeypatch):
    # 서비스까지 진짜로 태운다 — 이미지가 아닌 파일은 Vision 호출 없이 422, 사유는 로그에 남는다
    import app.api.routers.risk_detector as router_module
    import app.domain.risk_detector_service as service_module

    async def must_not_call(chunk, client=None):
        raise AssertionError("Vision이 호출되면 안 된다")
    monkeypatch.setattr(service_module, "acall_vision_api_with_usage", must_not_call)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(router_module, "log_event", lambda event, **fields: events.append((event, fields)))
    app.dependency_overrides[get_risk_detector_service] = lambda: service_module.RiskDetectorService()

    resp = await client.post(
        "/risk-detector/analyze", data=FORM_DATA, files=[("files", ("a.pdf", b"%PDF-1.4 fake", "application/pdf"))],
        headers={"x-user-id": "risk_guard_real_user"},
    )

    assert resp.status_code == 422
    assert "이미지 파일만" in resp.json()["detail"]
    assert [fields["reason"] for event, fields in events if event == "input_rejected"] == ["not_an_image"]
