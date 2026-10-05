from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.core.config import get_settings
from app.core.logging import log_event
from app.domain.estimate_engine import EstimateEngine
from app.domain.risk_detector_service import RiskDetectorService
from app.domain.unit_price_reference import UnitPriceReference
from app.repositories.case_repository import CaseRepository
from app.repositories.coefficient_repository import CoefficientRepository
from app.repositories.estimate_repository import EstimateRepository
from app.repositories.feedback_repository import FeedbackRepository
from app.repositories.pending_estimate_repository import PendingEstimateRepository
from app.repositories.risk_parse_cache_repository import RiskParseCacheRepository
from app.repositories.risk_report_repository import RiskReportRepository
from app.repositories.unit_price_repository import UnitPriceRepository


@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 수명주기에 묶어 Mongo client·임베딩 모델·엔진을 한 번만 생성한다.

    요청마다 새로 만들지 않고, 여기서 만든 인스턴스를 app.state에 저장해
    Depends(get_engine) 등으로 라우터에 주입한다.
    """
    settings = get_settings()

    mongo_client = AsyncIOMotorClient(
        settings.mongo_uri,
        maxPoolSize=settings.mongo_max_pool_size,
        serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
    )
    embedder = SentenceTransformer(settings.embed_model)
    # use_reranker=False면 CrossEncoder를 생성하지 않는다 — 단순히 호출을 건너뛰는 게 아니라
    # 약 2.2GB짜리 가중치를 내려받지도, 메모리에 올리지도 않는 게 목적이다.
    reranker = CrossEncoder(settings.reranker_model, max_length=512) if settings.use_reranker else None
    log_event("model_loaded", embed_model=settings.embed_model, use_reranker=settings.use_reranker)
    case_repository = CaseRepository(
        collection=mongo_client[settings.mongo_db_name]["estimate_cases"],
        settings=settings,
    )
    coefficient_repository = CoefficientRepository(
        collection=mongo_client[settings.mongo_db_name]["correction_coefficients"],
    )
    active_coefficients = await coefficient_repository.get_active()

    app.state.mongo_client = mongo_client
    app.state.engine = EstimateEngine(
        case_repository=case_repository,
        embedder=embedder,
        reranker=reranker,
        vector_candidate_pool=settings.vector_candidate_pool,
        coefficients=active_coefficients,
        window_includes_door=settings.estimate_window_includes_door,
    )
    app.state.estimate_repository = EstimateRepository(
        collection=mongo_client[settings.mongo_db_name]["estimates"],
    )
    app.state.feedback_repository = FeedbackRepository(
        collection=mongo_client[settings.mongo_db_name]["estimate_feedback"],
    )
    app.state.pending_estimate_repository = PendingEstimateRepository(
        collection=mongo_client[settings.mongo_db_name]["pending_estimates"],
    )
    app.state.risk_report_repository = RiskReportRepository(
        collection=mongo_client[settings.mongo_db_name]["risk_reports"],
    )
    parse_cache = (
        RiskParseCacheRepository(collection=mongo_client[settings.mongo_db_name]["risk_parse_cache"])
        if settings.risk_parse_cache_enabled
        else None
    )
    unit_price_table, quantity_table = await UnitPriceRepository(
        collection=mongo_client[settings.mongo_db_name]["unit_price_reference"]
    ).load()
    if not unit_price_table:
        # 표는 scripts/build_unit_price_reference.py가 만든다. 없으면 리스크 진단이 단가 지적 없이 돈다
        log_event("unit_price_reference_empty", level="warning")
    app.state.risk_detector_service = RiskDetectorService(
        unit_prices=UnitPriceReference(unit_price_table, quantity_table) if unit_price_table else None,
        vision_max_concurrency=settings.risk_vision_max_concurrency,
        vision_max_concurrency_per_request=settings.risk_vision_max_concurrency_per_request,
        parse_cache=parse_cache,
    )

    yield

    mongo_client.close()


def get_engine(request: Request) -> EstimateEngine:
    return request.app.state.engine


def get_estimate_repository(request: Request) -> EstimateRepository:
    return request.app.state.estimate_repository


def get_feedback_repository(request: Request) -> FeedbackRepository:
    return request.app.state.feedback_repository


def get_pending_estimate_repository(request: Request) -> PendingEstimateRepository:
    return request.app.state.pending_estimate_repository


def get_risk_report_repository(request: Request) -> RiskReportRepository:
    return request.app.state.risk_report_repository


def get_risk_detector_service(request: Request) -> RiskDetectorService:
    return request.app.state.risk_detector_service
