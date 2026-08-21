import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from app.api.routers import estimates
from app.core.deps import lifespan
from app.core.logging import configure_logging, log_event, set_request_id
from app.core.rate_limit import limiter

configure_logging()

app = FastAPI(title="Muneo AI", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)
app.include_router(estimates.router)


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
