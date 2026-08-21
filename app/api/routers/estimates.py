from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.core.deps import (
    get_engine,
    get_estimate_repository,
    get_feedback_repository,
    get_pending_estimate_repository,
)
from app.core.rate_limit import _global_key, limiter
from app.domain.estimate_engine import EstimateEngine
from app.repositories.estimate_repository import EstimateRepository
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.pending_estimate_repository import PendingEstimateRepository
from app.schemas.estimate import (
    EstimateRequest,
    EstimateResponse,
    FeedbackId,
    FeedbackRequest,
    SavedEstimateId,
    SavedEstimateSummary,
    SaveEstimateRequest,
)

router = APIRouter(prefix="/estimates", tags=["estimates"])

# Spring이 인증을 처리하고 이 헤더로 사용자를 식별해 넘겨준다 (FastAPI 쪽엔 자체 인증 로직 없음)
XUserId = Annotated[str, Header(alias="x-user-id")]


@router.post("/generate", response_model=EstimateResponse)
@limiter.limit("10/minute")  # 요청당 임베딩+cross-encoder 추론(~6초)이 붙는 무거운 엔드포인트라 별도로 더 세게 제한
@limiter.limit("100/minute", key_func=_global_key)  # x-user-id는 검증이 없어 값을 바꿔가며 사용자별
# 제한을 우회할 수 있음 — 신원과 무관하게 서버가 감당 못 할 총 요청량 자체를 막는 전역 상한
async def generate_estimate(
    request: Request,
    body: EstimateRequest,
    engine: Annotated[EstimateEngine, Depends(get_engine)],
    pending_repo: Annotated[PendingEstimateRepository, Depends(get_pending_estimate_repository)],
    x_user_id: XUserId,
) -> EstimateResponse:
    input_data = body.model_dump(exclude_none=True)
    result = await engine.generate(input_data)
    if "error" in result:
        raise HTTPException(status_code=422, detail=result["error"])

    # /save가 클라이언트 대신 이 값을 그대로 쓰도록 캐싱해두고 토큰만 발급 (SaveEstimateRequest 참고)
    token = await pending_repo.create(x_user_id, input_data, result)
    return EstimateResponse(**result, estimate_token=token)


@router.post("/save", status_code=201, response_model=SavedEstimateId)
async def save_estimate(
    body: SaveEstimateRequest,
    estimate_repo: Annotated[EstimateRepository, Depends(get_estimate_repository)],
    pending_repo: Annotated[PendingEstimateRepository, Depends(get_pending_estimate_repository)],
    x_user_id: XUserId,
) -> SavedEstimateId:
    pending = await pending_repo.consume(body.estimate_token, x_user_id)
    if pending is None:
        raise HTTPException(
            status_code=404,
            detail="유효하지 않거나 만료된 견적입니다. /estimates/generate를 다시 호출하세요.",
        )
    input_data, result = pending

    estimate_id = await estimate_repo.save(x_user_id, input_data, result)
    return SavedEstimateId(id=estimate_id)


@router.get("", response_model=list[SavedEstimateSummary])
async def list_estimates(
    repo: Annotated[EstimateRepository, Depends(get_estimate_repository)],
    x_user_id: XUserId,
) -> list[dict]:
    return await repo.list_by_user(x_user_id)


@router.delete("/{estimate_id}", status_code=204)
async def delete_estimate(
    estimate_id: str,
    repo: Annotated[EstimateRepository, Depends(get_estimate_repository)],
    x_user_id: XUserId,
) -> None:
    result = await repo.delete(estimate_id, x_user_id)
    if result is None:
        raise HTTPException(status_code=400, detail="유효하지 않은 ID입니다.")
    if not result:
        raise HTTPException(status_code=404, detail="견적을 찾을 수 없습니다.")


@router.post("/{estimate_id}/feedback", status_code=201, response_model=FeedbackId)
async def submit_feedback(
    estimate_id: str,
    body: FeedbackRequest,
    estimate_repo: Annotated[EstimateRepository, Depends(get_estimate_repository)],
    feedback_repo: Annotated[FeedbackRepository, Depends(get_feedback_repository)],
    x_user_id: XUserId,
) -> FeedbackId:
    """실제 계약금액을 기록한다 (정확도 피드백 루프). 본인 견적에만 남길 수 있다."""
    marked = await estimate_repo.mark_contracted(estimate_id, x_user_id)
    if not marked:
        raise HTTPException(status_code=404, detail="견적을 찾을 수 없습니다.")

    feedback_id = await feedback_repo.create(estimate_id, body.actual_cost, body.contracted_at)
    return FeedbackId(id=feedback_id)
