import time
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routers import estimates, risk_detector
from app.core.deps import lifespan
from app.core.logging import configure_logging, log_event, set_request_id
from app.core.rate_limit import limiter
from app.domain.risk_input_guard import MAX_REQUEST_BYTES

# app/core/config.py(pydantic-settings)는 .env를 읽어 Settings 객체에만 채우고 os.environ엔
# 안 넣는다. pipeline/vision_client.py의 anthropic.Anthropic()은 os.environ에서 직접
# ANTHROPIC_API_KEY를 찾으므로, 지금까지 이 값을 요구한 건 load_dotenv()를 스스로 부르는
# 독립 스크립트(scripts/run_ingest.py 등)뿐이었다 — 서버 프로세스에선 아무도 안 불러서
# 실서버로 Vision API를 처음 호출하는 리스크 진단 엔드포인트에서 인증 에러로 드러났다.
load_dotenv()

configure_logging()

app = FastAPI(title="Muneo AI", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.include_router(estimates.router)
app.include_router(risk_detector.router)


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """리스크 진단 업로드의 본문 크기 상한 — 라우터의 장수·용량 검문은 본문을 다 받은 뒤에야 돈다.

    Content-Length로만 본다. 길이를 안 밝히는 전송(chunked)은 여기서 못 막으므로 앞단(프록시)에도 상한이 있어야 한다.
    """
    if request.method == "POST" and request.url.path == "/risk-detector/analyze":
        length = request.headers.get("content-length", "")
        if length.isdigit() and int(length) > MAX_REQUEST_BYTES:
            log_event("input_rejected", level="warning", path=request.url.path, reason="body_too_large")
            return JSONResponse(status_code=413, content={"detail": "올린 파일이 너무 큽니다. 이미지 수나 용량을 줄여 주세요."})
    return await call_next(request)


# 나중에 등록한 미들웨어가 바깥에서 돈다 — 위에서 막은 요청도 http_request 로그에 남도록 이게 뒤에 온다
@app.middleware("http")
async def log_requests(request: Request, call_next):
    request_id = str(uuid.uuid4())
    set_request_id(request_id)
    request.state.request_id = request_id

    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 1)

    level = "warning" if response.status_code >= 400 else "info"
    log_event(
        "http_request",
        level=level,
        method=request.method,
        path=request.url.path,
        status_code=response.status_code,
        duration_ms=duration_ms,
    )
    response.headers["X-Request-Id"] = request_id
    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """응답은 FastAPI 기본(422 + detail) 그대로 두고, 무엇이 막혔는지만 로그에 남긴다 — 입력값 자체는 적지 않는다."""
    log_event(
        "input_rejected",
        level="warning",
        path=request.url.path,
        reason="schema",
        fields=[".".join(str(part) for part in error["loc"]) for error in exc.errors()][:20],
        error_types=[error["type"] for error in exc.errors()][:20],
    )
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    log_event(
        "unhandled_exception",
        level="error",
        method=request.method,
        path=request.url.path,
        error_type=type(exc).__name__,
        error=str(exc),
    )
    return JSONResponse(status_code=500, content={"detail": "내부 서버 오류가 발생했습니다."})


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}
