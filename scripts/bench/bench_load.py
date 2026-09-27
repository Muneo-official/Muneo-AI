"""
scripts/bench/bench_load.py — 동시 부하 테스트 (Vision mock 서버 전용, 비용 0).

동시 사용자 수를 단계별로 올리며(기본 1/5/10/20) 같은 케이스를 반복 요청하고, 단계마다
p50·p95·처리량·에러율을 잰다. Vision 지연은 mock이 실측 분포대로 재현하므로, 여기서 드러나는
병목은 우리 서버 쪽이다 — threadpool 포화, 이벤트 루프 블로킹, 리랭킹 CPU 경합.

locust 대신 직접 짠 이유: 새 의존성 없이 결과를 같은 JSON 형식으로 남겨 report.py가 단건
측정과 같은 페이지에 그리게 하려고. 실시간 대시보드가 필요해지면 그때 locust를 붙인다.

사용법 (먼저 python -m scripts.bench.server --mock-vision <latency 결과> 로 서버를 띄운다):
  python -m scripts.bench.bench_load --label baseline --case S3
  python -m scripts.bench.bench_load --label parallel --case S3 --concurrency 1,5,10,20,40
"""

import argparse
import asyncio
import pathlib
import sys
import time
from datetime import datetime

import httpx

from scripts.bench.common import (
    DEFAULT_CASES_FILE,
    DEFAULT_LOG_FILE,
    build_server_record,
    form_fields,
    git_commit,
    load_cases,
    log_size,
    percentile,
    read_events,
    upload_files,
    write_result,
)

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

ENDPOINT = "/risk-detector/analyze"
REQUEST_TIMEOUT_S = 1800


async def _worker(client: httpx.AsyncClient, case: dict, files: list, queue: asyncio.Queue, out: list) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        started = time.perf_counter()
        try:
            resp = await client.post(ENDPOINT, data=form_fields(case), files=files)
            out.append({
                "status": resp.status_code,
                "wall_s": round(time.perf_counter() - started, 3),
                "request_id": resp.headers.get("x-request-id"),
            })
        except httpx.HTTPError as e:
            out.append({"status": None, "wall_s": round(time.perf_counter() - started, 3), "error": repr(e)})


async def _run_level(base_url: str, case: dict, concurrency: int, total: int) -> tuple[list[dict], float]:
    files = upload_files(case)  # 디스크 I/O를 측정에서 빼려고 한 번만 읽는다
    queue: asyncio.Queue = asyncio.Queue()
    for i in range(total):
        queue.put_nowait(i)
    results: list[dict] = []
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=REQUEST_TIMEOUT_S, limits=limits) as client:
        started = time.perf_counter()
        await asyncio.gather(*(_worker(client, case, files, queue, results) for _ in range(concurrency)))
        duration = time.perf_counter() - started
    return results, duration


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--case", default="S3", help="부하에 쓸 케이스 id (단계 간 비교를 위해 하나로 고정)")
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--concurrency", default="1,5,10,20")
    parser.add_argument("--requests-per-user", type=int, default=3, help="단계별 총 요청 = 동시 사용자 × 이 값")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--log-file", type=pathlib.Path, default=DEFAULT_LOG_FILE)
    args = parser.parse_args()

    case = next((c for c in load_cases(args.cases) if c["id"] == args.case), None)
    if case is None:
        raise SystemExit(f"케이스 {args.case} 없음")

    info = httpx.get(f"{args.base_url}/bench/info", timeout=10).json()
    if info.get("vision") != "mock":
        raise SystemExit(f"서버가 mock 모드가 아닙니다: {info} — 실제 API로 부하를 걸면 비용이 크고 "
                         "Anthropic rate limit에 먼저 막힙니다. server.py --mock-vision으로 띄우세요.")

    offset = log_size(args.log_file)
    started_at = datetime.now().isoformat(timespec="seconds")
    levels = []
    for concurrency in [int(x) for x in args.concurrency.split(",")]:
        total = concurrency * args.requests_per_user
        print(f"동시 {concurrency}명 × {args.requests_per_user}회 = {total}건 ...", end=" ", flush=True)
        results, duration = asyncio.run(_run_level(args.base_url, case, concurrency, total))
        ok = [r["wall_s"] for r in results if r["status"] == 200]
        level = {
            "concurrency": concurrency,
            "total_requests": total,
            "success": len(ok),
            "error_rate": round(1 - len(ok) / total, 4),
            "duration_s": round(duration, 3),
            "throughput_rpm": round(len(ok) / duration * 60, 2),
            "p50_s": round(percentile(ok, 50), 3) if ok else None,
            "p95_s": round(percentile(ok, 95), 3) if ok else None,
            "max_s": round(max(ok), 3) if ok else None,
            "requests": results,
        }
        levels.append(level)
        print(f"p50 {level['p50_s']}s · p95 {level['p95_s']}s · {level['throughput_rpm']} req/min · "
              f"에러 {level['error_rate']:.0%}")

    ids = {r["request_id"] for lv in levels for r in lv["requests"] if r.get("request_id")}
    events = read_events(args.log_file, ids, offset)
    for lv in levels:
        for r in lv["requests"]:
            rid = r.get("request_id")
            rec = build_server_record(events.get(rid, [])) if rid else None
            # 부하 테스트에선 타임라인 전체 대신 구간 시간만 남긴다 (요청 수가 많아 파일이 커짐)
            r["server"] = rec and {k: rec[k] for k in ("parse_images_s", "rule_analyze_s", "price_check_s", "total_s")}

    path = write_result("load", args.label, {
        "kind": "load",
        "label": args.label,
        "started_at": started_at,
        "git_commit": git_commit(),
        "base_url": args.base_url,
        "server_info": info,
        "case": {k: case[k] for k in ("id", "description", "images", "chunk_counts", "total_chunks")},
        "levels": levels,
    })
    print(f"\n저장: {path}")


if __name__ == "__main__":
    main()
