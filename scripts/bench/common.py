"""리스크 진단 벤치마크 공용 유틸 — 로그 조인, 비용 환산, 퍼센타일, 청크 수 계산.

측정 계획·결과 기록: docs/RISK_DETECTOR_PERF_COST_LOG.md
서버 로그(risk_vision_call / risk_analyze_timing / http_request)는 커밋 1에서 넣은 계측이고,
여기서는 응답 헤더 X-Request-Id로 그 로그를 요청 단위로 다시 묶는다.
"""

import json
import math
import pathlib
import subprocess
from datetime import datetime

from PIL import Image

from pipeline.image_prep import MAX_PARSE_WIDTH, SPLIT_HEIGHT_THRESHOLD, split_vertically

BENCH_DIR = pathlib.Path("logs/bench")  # logs/는 gitignore — 크롤링 데이터 경로·파싱 결과가 섞여서
DEFAULT_CASES_FILE = BENCH_DIR / "cases.json"
DEFAULT_LOG_FILE = pathlib.Path("logs/app.log")

# USD / 1M tokens, Anthropic 1st-party 단가 (claude-api 스킬 모델표 캐시 2026-06-24, 2026-09-27 조회).
# input_tokens는 캐시되지 않은 입력만 센다 — 캐시 쓰기/읽기는 별도 필드로 따로 과금된다.
PRICING_CHECKED_AT = "2026-09-27"
PRICING_PER_MTOK = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-haiku-4-5": {"input": 1.0, "output": 5.0},
}
CACHE_WRITE_MULTIPLIER = 1.25  # 5분 TTL 기준
CACHE_READ_MULTIPLIER = 0.1

TIMING_EVENT = "risk_analyze_timing"
VISION_CALL_EVENT = "risk_vision_call"
HTTP_EVENT = "http_request"


def cost_usd(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
) -> float:
    price = PRICING_PER_MTOK[model]
    return (
        input_tokens * price["input"]
        + cache_creation_input_tokens * price["input"] * CACHE_WRITE_MULTIPLIER
        + cache_read_input_tokens * price["input"] * CACHE_READ_MULTIPLIER
        + output_tokens * price["output"]
    ) / 1_000_000


def percentile(values: list[float], p: float) -> float:
    """선형 보간 퍼센타일 (p: 0~100). 표본이 3~20개 수준이라 보간이 없으면 p95가 곧 max가 된다."""
    if not values:
        return math.nan
    s = sorted(values)
    k = (len(s) - 1) * p / 100
    lo, hi = math.floor(k), math.ceil(k)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def count_chunks(width: int, height: int) -> int:
    """pipeline.image_prep._prepare_chunks()가 이 크기의 이미지를 몇 청크로 쪼갤지 — API 호출 없이.

    리사이즈 후 높이로 판정하고, 분할 루프는 split_vertically()를 1px 폭 더미 이미지에 그대로
    돌려서 센다(분할 규칙을 여기에 복제하지 않으려고).
    """
    if width > MAX_PARSE_WIDTH:
        height = int(height * MAX_PARSE_WIDTH / width)
    if height <= SPLIT_HEIGHT_THRESHOLD:
        return 1
    return len(split_vertically(Image.new("1", (1, height))))


def log_size(log_file: pathlib.Path) -> int:
    return log_file.stat().st_size if log_file.exists() else 0


def _iter_events(log_file: pathlib.Path, offset: int):
    """offset(측정 시작 시점의 파일 크기)부터 JSON 로그를 한 줄씩 읽는다.

    로그가 회전돼 파일이 offset보다 작아졌으면 처음부터 읽는다.
    """
    if not log_file.exists():
        return
    with log_file.open("rb") as f:
        if log_size(log_file) >= offset:
            f.seek(offset)
        for raw in f:
            try:
                yield json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue


def read_events(log_file: pathlib.Path, request_ids: set[str], offset: int = 0) -> dict[str, list[dict]]:
    """offset 이후 로그에서 주어진 request_id들의 이벤트를 요청별로 모은다."""
    by_request: dict[str, list[dict]] = {rid: [] for rid in request_ids}
    for event in _iter_events(log_file, offset):
        rid = event.get("request_id")
        if rid in by_request:
            by_request[rid].append(event)
    return by_request


def read_events_named(log_file: pathlib.Path, event_name: str, offset: int = 0) -> list[dict]:
    """offset 이후 로그에서 특정 이벤트를 전부 모은다 (request_id를 모르는 k6 부하 측정용)."""
    return [e for e in _iter_events(log_file, offset) if e.get("event") == event_name]


def event_ts(event: dict) -> float:
    return datetime.fromisoformat(event["timestamp"]).timestamp()


def build_server_record(events: list[dict]) -> dict | None:
    """한 요청의 서버 로그 → 구간 시간 + Vision 호출 타임라인(요청 시작 기준 오프셋).

    http_request 로그는 응답 직전에 찍히므로 (timestamp - duration_ms)가 요청 시작 시각이다.
    risk_vision_call은 호출이 끝난 직후 찍히므로 (timestamp - latency_s)가 호출 시작 시각이다.
    """
    timing = next((e for e in events if e["event"] == TIMING_EVENT), None)
    http = next((e for e in events if e["event"] == HTTP_EVENT), None)
    if timing is None or http is None:
        return None

    request_start = event_ts(http) - http["duration_ms"] / 1000
    calls = []
    for e in events:
        if e["event"] != VISION_CALL_EVENT:
            continue
        start = event_ts(e) - e["latency_s"] - request_start
        calls.append({
            "image_index": e["image_index"],
            "chunk_index": e["chunk_index"],
            "start_s": round(max(start, 0.0), 3),
            "latency_s": e["latency_s"],
            "slot_wait_s": e.get("slot_wait_s", 0.0),
            "input_tokens": e["input_tokens"],
            "output_tokens": e["output_tokens"],
            "cache_creation_input_tokens": e.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": e.get("cache_read_input_tokens", 0),
        })
    calls.sort(key=lambda c: (c["image_index"], c["chunk_index"]))

    keys = (
        "image_count", "chunk_count", "line_item_count", "parse_images_s", "vision_latency_sum_s",
        "rule_analyze_s", "price_check_s", "total_s", "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
    )
    return {
        "server_duration_s": round(http["duration_ms"] / 1000, 3),
        **{k: timing.get(k) for k in keys},
        "vision_calls": calls,
    }


def result_signature(response_json: dict) -> dict:
    """정확도 비교용 요약 — 개선 전후 결과가 같은지 이것끼리 비교한다."""
    report = response_json["report"]
    issues = sorted(
        (s["process"], item["status"], item["title"])
        for s in report["process_sections"]
        for item in s["items"]
        if item["status"] != "정상"
    )
    normal_count = sum(
        1 for s in report["process_sections"] for item in s["items"] if item["status"] == "정상"
    )
    return {
        "total_risk_items": report["summary"]["total_risk_items"],
        "chips": report["summary"]["chips"],
        "normal_item_count": normal_count,
        "issues": [list(i) for i in issues],
    }


def git_commit() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def load_cases(path: pathlib.Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def form_fields(case: dict) -> dict[str, str]:
    return {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in case["form"].items()}


def upload_files(case: dict) -> list[tuple[str, tuple[str, bytes, str]]]:
    files = []
    for path in case["images"]:
        p = pathlib.Path(path)
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        files.append(("files", (p.name, p.read_bytes(), mime)))
    return files


def write_result(kind: str, label: str, payload: dict, out_dir: pathlib.Path = BENCH_DIR) -> pathlib.Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = out_dir / f"{kind}_{label}_{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
