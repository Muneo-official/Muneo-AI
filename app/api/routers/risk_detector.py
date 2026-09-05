from typing import Annotated

from fastapi import APIRouter, Body, Depends, File, Form, Header, HTTPException, Request, UploadFile

from app.core.deps import get_risk_detector_service, get_risk_report_repository
from app.core.rate_limit import _global_key, limiter
from app.domain.risk_detector_service import RiskDetectorService
from app.repositories.risk_report_repository import RiskReportRepository
from app.schemas.risk import AnalyzeRiskCommand, AnalyzeRiskResponse

router = APIRouter(prefix="/risk-detector", tags=["risk-detector"])

# Spring이 인증을 처리하고 이 헤더로 사용자를 식별해 넘겨준다 (app/api/routers/estimates.py와 동일)
XUserId = Annotated[str, Header(alias="x-user-id")]

_SAMPLE_INPUT = {
    "space_type": "아파트",
    "pyeong": 30,
    "room_count": 3,
    "floor": 8,
    "elevator": True,
    "region": "서울",
    "building_age": "20년이상",
    "company_name": "홍길동 인테리어",
}

_SAMPLE_RESULT = AnalyzeRiskResponse.model_config["json_schema_extra"]["example"]


@router.post("/analyze", response_model=AnalyzeRiskResponse)
@limiter.limit("5/minute")  # 이미지 여러 장 * 청크당 Vision API 호출이라 /estimates/generate(10/min)보다 무거움
@limiter.limit("50/minute", key_func=_global_key)  # x-user-id 미검증 우회 방지용 전역 상한
async def analyze_risk(
    request: Request,
    service: Annotated[RiskDetectorService, Depends(get_risk_detector_service)],
    space_type: str = Form(..., description="아파트|빌라|오피스텔|단독주택"),
    pyeong: int = Form(..., description="면적(평)"),
    room_count: int = Form(..., description="방 개수"),
    floor: int = Form(..., description="층수"),
    elevator: bool = Form(..., description="엘리베이터 유무 (true/false)"),
    region: str = Form(..., description="지역"),
    building_age: str = Form(..., description="건물 연식"),
    company_name: str = Form(..., description="업체명"),
    files: list[UploadFile] = File(..., description="견적서 이미지 여러개 업로드 가능"),
):
    try:
        image_bytes = [await f.read() for f in files]
        command = AnalyzeRiskCommand(
            space_type=space_type,
            pyeong=pyeong,
            room_count=room_count,
            floor=floor,
            elevator=elevator,
            region=region,
            building_age=building_age,
            company_name=company_name,
            image_files=image_bytes,
        )
        return await service.analyze(command)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"리스크 분석 실패: {e}")


@router.post("/save", status_code=201)
async def save_report(
    repo: Annotated[RiskReportRepository, Depends(get_risk_report_repository)],
    x_user_id: XUserId,
    body: dict = Body(examples=[{"input": _SAMPLE_INPUT, "result": _SAMPLE_RESULT}]),
):
    user_input = body.get("input")
    result = body.get("result")
    if not user_input or not result:
        raise HTTPException(status_code=400, detail="input과 result가 필요합니다.")

    report_id = await repo.save(x_user_id, user_input, result)
    return {"id": report_id}


@router.get("")
async def get_reports(
    repo: Annotated[RiskReportRepository, Depends(get_risk_report_repository)],
    x_user_id: XUserId,
):
    return await repo.list_by_user(x_user_id)


@router.delete("/{report_id}", status_code=204)
async def remove_report(
    report_id: str,
    repo: Annotated[RiskReportRepository, Depends(get_risk_report_repository)],
    x_user_id: XUserId,
):
    result = await repo.delete(report_id, x_user_id)
    if result is None:
        raise HTTPException(status_code=400, detail="유효하지 않은 ID입니다.")
    if not result:
        raise HTTPException(status_code=404, detail="리스크 진단 리포트를 찾을 수 없습니다.")
