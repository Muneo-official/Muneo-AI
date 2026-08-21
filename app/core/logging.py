import json
import logging
import pathlib
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler

logger = logging.getLogger("muneo")

LOG_DIR = pathlib.Path("logs")
LOG_FILE = LOG_DIR / "app.log"

# 요청 하나 안에서 발생하는 여러 log_event 호출(http_request, retrieve_cases, hybrid_rerank,
# generate_success 등)을 한 request_id로 묶어서 나중에 grep/jq로 한 요청의 전체 흐름을 추적할 수 있게 한다.
_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)


def set_request_id(request_id: str) -> None:
    _request_id.set(request_id)


def configure_logging(level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    # 파일로도 남긴다 — Docker/EC2에서 stdout만으론 컨테이너 재시작 시 로그가 날아갈 수 있어서,
    # 별도 로그 수집기(CloudWatch 등) 없이도 최근 이력을 로컬에서 볼 수 있게 함.
    # 10MB × 5개 보관, 그 이상은 자동 회전 삭제.
    LOG_DIR.mkdir(exist_ok=True)
    handlers.append(RotatingFileHandler(LOG_FILE, maxBytes=10_000_000, backupCount=5, encoding="utf-8"))

    for handler in handlers:
        handler.setFormatter(logging.Formatter("%(message)s"))

    logger.handlers = handlers
    logger.setLevel(level)
    logger.propagate = False


def log_event(event: str, level: str = "info", **fields) -> None:
    """구조화 로그 한 줄(JSON)을 남긴다.

    fallback stage, 응답 시간, 참고 사례 수처럼 나중에 리랭킹/하이브리드 검색
    적용 전후를 비교할 때 근거가 되는 값들을 여기로 기록한다.

    level: "info" | "warning" | "error" — scripts/log_report.py가 이 값으로 에러율을 집계한다.
    """
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "event": event,
        "level": level,
        "request_id": _request_id.get(),
        **fields,
    }
    log_fn = {"info": logger.info, "warning": logger.warning, "error": logger.error}[level]
    log_fn(json.dumps(payload, ensure_ascii=False, default=str))
