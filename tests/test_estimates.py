"""
라우터 계층 테스트 — Mongo/임베딩 모델 없이 API 배선(요청 검증 → 엔진 호출 → 응답 변환)만 검증한다.

get_engine 의존성을 mock으로 오버라이드하므로 실제 lifespan(Mongo 연결, 모델 로드)은
트리거되지 않는다. 엔진 자체의 정확도는 여기서 다루지 않는다 — Mongo 연결 이후
별도 도메인 테스트로 검증한다.
"""

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.deps import (
    get_engine,
    get_estimate_repository,
    get_feedback_repository,
    get_pending_estimate_repository,
)
from app.main import app

SAMPLE_RESULT = {
    "총_견적_범위": {"최소": 10_000_000, "중간": 15_000_000, "최대": 20_000_000},
    "공종별_단가_범위": {},
    "공종별_항목_명세": {},
    "보정_적용": [],
    "시공범위": "부분",
    "선택_공종": ["도배"],
    "참고_사례_수": 5,
    "참고_사례": [],
    "검색_쿼리": "30평 서울 아파트 도배 리모델링",
    "engine_version": "1.0.0",
    "coefficient_version": "1.0.0",
    "reference_case_ids": ["111111", "222222"],
}

VALID_REQUEST = {"공종": ["도배"], "평수": 30}
USER_HEADER = {"x-user-id": "user_abc"}


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


def _mock_pending_repo(token: str = "tok_abc123") -> AsyncMock:
    repo = AsyncMock()
    repo.create.return_value = token
    return repo


async def test_health(client: AsyncClient):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_generate_estimate_success(client: AsyncClient):
    mock_engine = AsyncMock()
    mock_engine.generate.return_value = SAMPLE_RESULT
    app.dependency_overrides[get_engine] = lambda: mock_engine
    app.dependency_overrides[get_pending_estimate_repository] = lambda: _mock_pending_repo()

    resp = await client.post("/estimates/generate", json=VALID_REQUEST, headers=USER_HEADER)

    assert resp.status_code == 200
    body = resp.json()
    assert body["총_견적_범위"]["중간"] == 15_000_000
    assert body["참고_사례_수"] == 5
    assert body["estimate_token"] == "tok_abc123"  # /save가 이 토큰으로 결과를 그대로 저장한다
    mock_engine.generate.assert_awaited_once()


async def test_generate_estimate_no_match_returns_422(client: AsyncClient):
    mock_engine = AsyncMock()
    mock_engine.generate.return_value = {"error": "유사 사례를 찾을 수 없습니다."}
    app.dependency_overrides[get_engine] = lambda: mock_engine
    app.dependency_overrides[get_pending_estimate_repository] = lambda: _mock_pending_repo()

    resp = await client.post("/estimates/generate", json=VALID_REQUEST, headers=USER_HEADER)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "유사 사례를 찾을 수 없습니다."


async def test_generate_estimate_missing_user_header_returns_422(client: AsyncClient):
    mock_engine = AsyncMock()
    app.dependency_overrides[get_engine] = lambda: mock_engine
    app.dependency_overrides[get_pending_estimate_repository] = lambda: _mock_pending_repo()

    resp = await client.post("/estimates/generate", json=VALID_REQUEST)

    assert resp.status_code == 422
    mock_engine.generate.assert_not_awaited()


async def test_generate_estimate_rate_limited_after_10_per_minute(client: AsyncClient):
    # /estimates/generate는 요청당 무거운 추론이 붙어서 x-user-id별로 10/minute로 별도 제한된다.
    mock_engine = AsyncMock()
    mock_engine.generate.return_value = SAMPLE_RESULT
    app.dependency_overrides[get_engine] = lambda: mock_engine
    app.dependency_overrides[get_pending_estimate_repository] = lambda: _mock_pending_repo()
    headers = {"x-user-id": "rate_limit_test_user"}

    statuses = []
    for _ in range(11):
        resp = await client.post("/estimates/generate", json=VALID_REQUEST, headers=headers)
        statuses.append(resp.status_code)

    assert statuses[:10] == [200] * 10
    assert statuses[10] == 429


async def test_generate_estimate_invalid_body_returns_422(client: AsyncClient):
    # 평수는 필수(gt=0) — 누락 시 엔진 호출 전에 Pydantic이 막아야 한다.
    mock_engine = AsyncMock()
    app.dependency_overrides[get_engine] = lambda: mock_engine
    app.dependency_overrides[get_pending_estimate_repository] = lambda: _mock_pending_repo()

    resp = await client.post("/estimates/generate", json={"공종": ["도배"]}, headers=USER_HEADER)

    assert resp.status_code == 422
    mock_engine.generate.assert_not_awaited()


async def test_generate_estimate_invalid_wallpaper_range_returns_422(client: AsyncClient):
    # 도배.범위는 전체/거실/침실/주방만 허용 — 그 외 값(예: "부분")은 엔진에서 5% 최저치로
    # 조용히 폴백되던 과거 버그가 있었다. 이제 Pydantic 단에서 막아야 한다.
    mock_engine = AsyncMock()
    app.dependency_overrides[get_engine] = lambda: mock_engine
    app.dependency_overrides[get_pending_estimate_repository] = lambda: _mock_pending_repo()

    body = {**VALID_REQUEST, "도배": {"범위": "부분"}}
    resp = await client.post("/estimates/generate", json=body, headers=USER_HEADER)

    assert resp.status_code == 422
    mock_engine.generate.assert_not_awaited()


async def test_save_estimate_success(client: AsyncClient):
    mock_estimate_repo = AsyncMock()
    mock_estimate_repo.save.return_value = "665f1a2b3c4d5e6f7a8b9c0d"
    mock_pending_repo = AsyncMock()
    mock_pending_repo.consume.return_value = (VALID_REQUEST, SAMPLE_RESULT)
    app.dependency_overrides[get_estimate_repository] = lambda: mock_estimate_repo
    app.dependency_overrides[get_pending_estimate_repository] = lambda: mock_pending_repo

    resp = await client.post(
        "/estimates/save", json={"estimate_token": "tok_abc123"}, headers=USER_HEADER,
    )

    assert resp.status_code == 201
    assert resp.json() == {"id": "665f1a2b3c4d5e6f7a8b9c0d"}
    mock_pending_repo.consume.assert_awaited_once_with("tok_abc123", "user_abc")
    mock_estimate_repo.save.assert_awaited_once_with("user_abc", VALID_REQUEST, SAMPLE_RESULT)


async def test_save_estimate_expired_token_returns_404(client: AsyncClient):
    # 토큰이 없거나(만료돼서 TTL로 이미 삭제됨) 다른 사용자 것이면 consume()이 None을 반환한다
    mock_estimate_repo = AsyncMock()
    mock_pending_repo = AsyncMock()
    mock_pending_repo.consume.return_value = None
    app.dependency_overrides[get_estimate_repository] = lambda: mock_estimate_repo
    app.dependency_overrides[get_pending_estimate_repository] = lambda: mock_pending_repo

    resp = await client.post(
        "/estimates/save", json={"estimate_token": "expired_or_stolen"}, headers=USER_HEADER,
    )

    assert resp.status_code == 404
    mock_estimate_repo.save.assert_not_awaited()


async def test_save_estimate_missing_user_header_returns_422(client: AsyncClient):
    mock_estimate_repo = AsyncMock()
    mock_pending_repo = AsyncMock()
    app.dependency_overrides[get_estimate_repository] = lambda: mock_estimate_repo
    app.dependency_overrides[get_pending_estimate_repository] = lambda: mock_pending_repo

    resp = await client.post("/estimates/save", json={"estimate_token": "tok_abc123"})

    assert resp.status_code == 422
    mock_estimate_repo.save.assert_not_awaited()


async def test_list_estimates_success(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.list_by_user.return_value = [
        {
            "id": "665f1a2b3c4d5e6f7a8b9c0d",
            "user_id": "user_abc",
            "created_at": "2026-08-16T00:00:00Z",
            "input": VALID_REQUEST,
            "result": {k: v for k, v in SAMPLE_RESULT.items() if k not in ("참고_사례", "참고_사례_수", "검색_쿼리")},
            "status": "saved",
            "valid_until": "2026-09-15T00:00:00Z",
        }
    ]
    app.dependency_overrides[get_estimate_repository] = lambda: mock_repo

    resp = await client.get("/estimates", headers=USER_HEADER)

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["id"] == "665f1a2b3c4d5e6f7a8b9c0d"
    mock_repo.list_by_user.assert_awaited_once_with("user_abc")


async def test_delete_estimate_success(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.delete.return_value = True
    app.dependency_overrides[get_estimate_repository] = lambda: mock_repo

    resp = await client.delete("/estimates/665f1a2b3c4d5e6f7a8b9c0d", headers=USER_HEADER)

    assert resp.status_code == 204


async def test_delete_estimate_not_found_returns_404(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.delete.return_value = False
    app.dependency_overrides[get_estimate_repository] = lambda: mock_repo

    resp = await client.delete("/estimates/665f1a2b3c4d5e6f7a8b9c0d", headers=USER_HEADER)

    assert resp.status_code == 404


async def test_delete_estimate_invalid_id_returns_400(client: AsyncClient):
    mock_repo = AsyncMock()
    mock_repo.delete.return_value = None

    app.dependency_overrides[get_estimate_repository] = lambda: mock_repo

    resp = await client.delete("/estimates/not-a-valid-id", headers=USER_HEADER)

    assert resp.status_code == 400


FEEDBACK_BODY = {"actual_cost": 18_500_000, "contracted_at": "2026-08-01T00:00:00Z"}


async def test_submit_feedback_success(client: AsyncClient):
    mock_estimate_repo = AsyncMock()
    mock_estimate_repo.mark_contracted.return_value = True
    mock_feedback_repo = AsyncMock()
    mock_feedback_repo.create.return_value = "775f1a2b3c4d5e6f7a8b9c0d"
    app.dependency_overrides[get_estimate_repository] = lambda: mock_estimate_repo
    app.dependency_overrides[get_feedback_repository] = lambda: mock_feedback_repo

    resp = await client.post(
        "/estimates/665f1a2b3c4d5e6f7a8b9c0d/feedback",
        json=FEEDBACK_BODY,
        headers=USER_HEADER,
    )

    assert resp.status_code == 201
    assert resp.json() == {"id": "775f1a2b3c4d5e6f7a8b9c0d"}
    mock_estimate_repo.mark_contracted.assert_awaited_once_with("665f1a2b3c4d5e6f7a8b9c0d", "user_abc")
    mock_feedback_repo.create.assert_awaited_once()


async def test_submit_feedback_estimate_not_found_returns_404(client: AsyncClient):
    mock_estimate_repo = AsyncMock()
    mock_estimate_repo.mark_contracted.return_value = False
    mock_feedback_repo = AsyncMock()
    app.dependency_overrides[get_estimate_repository] = lambda: mock_estimate_repo
    app.dependency_overrides[get_feedback_repository] = lambda: mock_feedback_repo

    resp = await client.post(
        "/estimates/665f1a2b3c4d5e6f7a8b9c0d/feedback",
        json=FEEDBACK_BODY,
        headers=USER_HEADER,
    )

    assert resp.status_code == 404
    mock_feedback_repo.create.assert_not_awaited()
