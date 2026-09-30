"""리스크 진단 벤치마크 공용 유틸 — 로그 조인, 비용 환산, 퍼센타일, 청크 수 계산.

측정 계획·결과 기록: docs/RISK_DETECTOR_PERF_COST_LOG.md
서버 로그(risk_vision_call / risk_analyze_timing / http_request)는 커밋 1에서 넣은 계측이고,
여기서는 응답 헤더 X-Request-Id로 그 로그를 요청 단위로 다시 묶는다.
"""

import base64
import hashlib
import json
import math
import pathlib
import subprocess
from collections import defaultdict
from datetime import datetime

from PIL import Image

from pipeline.image_prep import (
    MAX_PARSE_WIDTH,
    SPLIT_HEIGHT_THRESHOLD,
    prepare_chunks_from_bytes,
    split_vertically,
)
from pipeline.parsing import _safe_int

BENCH_DIR = pathlib.Path("logs/bench")  # logs/는 gitignore — 크롤링 데이터 경로·파싱 결과가 섞여서
DEFAULT_CASES_FILE = BENCH_DIR / "cases.json"
DEFAULT_LOG_FILE = pathlib.Path("logs/app.log")

# USD / 1M tokens, Anthropic 1st-party 단가 (claude-api 스킬 모델표 캐시 2026-09-25, 2026-09-30 조회).
# input_tokens는 캐시되지 않은 입력만 센다 — 캐시 쓰기/읽기는 별도 필드로 따로 과금된다.
PRICING_CHECKED_AT = "2026-09-30"
PRICING_PER_MTOK = {
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-sonnet-5": {"input": 2.0, "output": 10.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0},
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
            "tool_called": e.get("tool_called", True),  # 필드 도입 전 로그는 강제 도구 호출이라 항상 호출됨
        })
    calls.sort(key=lambda c: (c["image_index"], c["chunk_index"]))

    keys = (
        "image_count", "chunk_count", "line_item_count", "parse_images_s", "vision_latency_sum_s",
        "rule_analyze_s", "price_check_s", "total_s", "input_tokens", "output_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        # 이미지 파싱 캐시 (캐시 도입 전 로그엔 없어서 None)
        "parse_cache_hits", "parse_cache_misses", "parse_cache_saved_input_tokens",
        "parse_cache_saved_output_tokens", "parse_cache_lookup_s", "parse_cache_store_s",
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


# ── 파싱 결과 캡처 (비용 작업, docs/RISK_DETECTOR_COST_LOG.md) ──
# 출력 스키마를 줄이면 모델 출력 자체가 바뀐다. 응답 JSON엔 공종별 금액이 없어서, bench 서버(real 모드)가
# 청크별 원본 모델 출력과 룰 분석에 들어간 최종 항목을 request_id별로 모아 두고 bench_latency가 가져간다.
# 청크는 이미지 데이터 해시로 식별한다 — 병렬 호출이라 도착 순서로는 (이미지, 청크)를 알 수 없다.


def chunk_digest(image_b64: str) -> str:
    return hashlib.sha256(image_b64.encode("ascii")).hexdigest()[:16]


def params_image_digest(params: dict) -> str | None:
    """messages.create 파라미터에서 첫 이미지 블록의 해시. 프롬프트 순서가 바뀌어도 찾도록 타입으로 찾는다."""
    for message in params.get("messages", []):
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "image":
                return chunk_digest(block["source"]["data"])
    return None


def case_chunk_digests(case: dict) -> dict[str, list[tuple[int, int]]]:
    """케이스 이미지를 서버와 같은 전처리로 청크 분할해 해시 → [(이미지, 청크)] 로 만든다."""
    index: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for image_index, path in enumerate(case["images"]):
        chunks = prepare_chunks_from_bytes(pathlib.Path(path).read_bytes())
        for chunk_index, chunk in enumerate(chunks):
            index[chunk_digest(base64.standard_b64encode(chunk).decode("ascii"))].append((image_index, chunk_index))
    return dict(index)


def assign_chunk_indices(calls: list[dict], digests: dict[str, list[tuple[int, int]]]) -> tuple[list[dict], int]:
    """캡처된 호출에 (이미지, 청크) 인덱스를 붙여 그 순서로 정렬한다. 반환: (정렬된 호출, 매칭 실패 수).

    같은 청크가 두 번 나오면(같은 이미지를 두 장 올린 경우) 앞 인덱스부터 차례로 배정한다.
    """
    remaining = {k: list(v) for k, v in digests.items()}
    assigned, unmatched = [], 0
    for call in calls:
        slots = remaining.get(call.get("chunk_digest"))
        if not slots:
            unmatched += 1
            continue
        image_index, chunk_index = slots.pop(0)
        assigned.append({"image_index": image_index, "chunk_index": chunk_index, **call})
    assigned.sort(key=lambda c: (c["image_index"], c["chunk_index"]))
    return assigned, unmatched


def parse_metrics(line_items: list[dict], vision_calls: list[dict]) -> dict:
    """정확도 비교 지표 — 항목 수, 전체·공종별 금액 합계, total_cost.

    공종별 금액은 가격 체크(risk_price_checker._sum_amount_by_category)의 입력과 같은 방식으로 합산한다.
    total_cost는 병합 로직(merge_chunk_results)처럼 이미지마다 청크 중 최댓값을 잡아 이미지끼리 더한다.
    """
    by_category: dict[str, int] = defaultdict(int)
    for item in line_items:
        if item.get("category") and item.get("amount"):
            by_category[item["category"]] += _safe_int(item["amount"])
    per_image_total: dict[int, int] = defaultdict(int)
    for call in vision_calls:
        output = call.get("output") or {}
        if output.get("is_estimate"):
            per_image_total[call["image_index"]] = max(
                per_image_total[call["image_index"]], _safe_int(output.get("total_cost"))
            )
    return {
        "parsed_item_count": len(line_items),
        "amount_sum": sum(by_category.values()),
        "category_amounts": dict(sorted(by_category.items())),
        "total_cost": sum(per_image_total.values()),
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
