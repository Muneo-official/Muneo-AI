"""
scripts/bench/k6_result.py — k6 부하 테스트 결과를 report.py 형식으로 변환하고 서버 로그를 붙인다.

k6(scripts/bench/load_test.js)는 직접 실행한다. 이 스크립트는 그 결과 파일 두 개를 읽는다.
  - --out json (요청 단위 기록): 단계(시나리오)별 p50/p95·처리량·에러율과 단계의 시간 구간
  - --summary-export: 단계별 threshold 통과 여부 (이 파일에선 true가 "기준 초과 = 실패"다)
그리고 단계의 시간 구간에 서버가 남긴 risk_analyze_timing 로그로 서버 안쪽 시간을 집계한다.
클라이언트 p50과 서버 p50의 차이 = 서버에 들어가기 전 대기(업로드 파싱·스레드 대기 등).

k6는 요청 기록의 time을 요청 종료 시각으로 찍는다 → 시작 = time - http_req_duration.
load_test.js가 단계 사이에 간격을 두므로 시간 구간으로 나눠도 단계끼리 섞이지 않는다.

사용법:
  k6 run -e CASE=S3 --out json=logs/bench/k6_raw.json --summary-export=logs/bench/k6_summary.json scripts/bench/load_test.js
  python -m scripts.bench.k6_result --label baseline
"""

import argparse
import json
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

import httpx

from scripts.bench.common import (
    BENCH_DIR,
    DEFAULT_CASES_FILE,
    DEFAULT_LOG_FILE,
    TIMING_EVENT,
    event_ts,
    git_commit,
    load_cases,
    percentile,
    read_events_named,
    write_result,
)

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

WINDOW_MARGIN_S = 2.0  # 서버 로그 시각과 k6 시각의 미세한 차이 흡수


def scenario_meta(name: str) -> dict:
    """load_test.js의 시나리오 이름 규칙: c<동시 사용자 수> (closed) / r<분당 도착 건수> (open)."""
    m = re.fullmatch(r"([cr])(\d+)", name)
    if not m:
        raise ValueError(f"알 수 없는 시나리오 이름: {name}")
    n = int(m.group(2))
    return {"model": "closed", "concurrency": n} if m.group(1) == "c" else {"model": "open", "arrival_rpm": n}


def read_points(raw_path: pathlib.Path) -> list[dict]:
    points = []
    with raw_path.open(encoding="utf-8") as f:
        for line in f:
            p = json.loads(line)
            if p.get("type") == "Point":
                points.append(p)
    return points


def levels_from_points(points: list[dict]) -> dict[str, dict]:
    """요청 단위 기록 → 시나리오별 통계와 시간 구간 (epoch 초)."""
    requests: dict[str, list[tuple[float, float, bool]]] = defaultdict(list)  # (시작, 종료, 성공)
    dropped: dict[str, int] = defaultdict(int)
    failed_flags: dict[str, list[int]] = defaultdict(list)
    for p in points:
        tags = p["data"].get("tags") or {}
        scenario = tags.get("scenario")
        if not scenario or scenario == "setup":
            continue
        metric, value = p["metric"], p["data"]["value"]
        if metric == "http_req_duration":
            end = datetime.fromisoformat(p["data"]["time"]).timestamp()
            ok = tags.get("expected_response") == "true"
            requests[scenario].append((end - value / 1000, end, ok))
        elif metric == "http_req_failed":
            failed_flags[scenario].append(int(value))
        elif metric == "dropped_iterations":
            dropped[scenario] += int(value)

    levels = {}
    for scenario in set(requests) | set(dropped):
        reqs = requests.get(scenario, [])
        ok_durations = [end - start for start, end, ok in reqs if ok]
        success = len(ok_durations)
        flags = failed_flags.get(scenario, [])
        start = min((s for s, _, _ in reqs), default=None)
        end = max((e for _, e, _ in reqs), default=None)
        window = (end - start) if reqs else 0.0
        levels[scenario] = {
            **scenario_meta(scenario),
            "total_requests": len(reqs),
            "success": success,
            "error_rate": round(sum(flags) / len(flags), 4) if flags else 0.0,
            "dropped_iterations": dropped.get(scenario, 0),
            "duration_s": round(window, 3),
            "throughput_rpm": round(success / window * 60, 2) if window else 0.0,
            "p50_s": round(percentile(ok_durations, 50), 3) if ok_durations else None,
            "p90_s": round(percentile(ok_durations, 90), 3) if ok_durations else None,
            "p95_s": round(percentile(ok_durations, 95), 3) if ok_durations else None,
            "max_s": round(max(ok_durations), 3) if ok_durations else None,
            "window": (start, end),
        }
    return levels


def thresholds_by_scenario(summary: dict) -> dict[str, dict[str, bool]]:
    """summary-export의 threshold → {시나리오: {"metric{scenario:x}: expr": 통과 여부}}.

    summary-export는 기준을 넘었을 때(실패) true를 기록한다 — 통과 여부로 뒤집어 저장한다.
    """
    out: dict[str, dict[str, bool]] = defaultdict(dict)
    for metric_name, metric in summary.get("metrics", {}).items():
        m = re.search(r"\{scenario:([^}]+)\}", metric_name)
        if not m:
            continue
        for expr, crossed in (metric.get("thresholds") or {}).items():
            out[m.group(1)][f"{metric_name}: {expr}"] = not crossed
    return dict(out)


def server_side_stats(timing_events: list[dict]) -> dict | None:
    """같은 구간에 처리된 요청들의 서버 안쪽 시간 중앙값."""
    if not timing_events:
        return None

    def med(key: str) -> float:
        return round(statistics.median(e[key] for e in timing_events), 3)

    return {
        "requests": len(timing_events),
        "parse_images_p50_s": med("parse_images_s"),
        "price_check_p50_s": med("price_check_s"),
        "total_p50_s": med("total_s"),
    }


def _k6_version() -> str:
    exe = shutil.which("k6") or r"C:\Program Files\k6\k6.exe"
    try:
        out = subprocess.run([exe, "version"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return "k6"
    m = re.search(r"v\d+\.\d+\.\d+", out)
    return f"k6 {m.group(0)}" if m else "k6"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--raw", type=pathlib.Path, default=BENCH_DIR / "k6_raw.json", help="k6 --out json 파일")
    parser.add_argument("--summary", type=pathlib.Path, default=BENCH_DIR / "k6_summary.json", help="k6 --summary-export 파일")
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--log-file", type=pathlib.Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="mock 설정 기록용 (서버가 꺼져 있어도 됨)")
    args = parser.parse_args()

    if not args.raw.exists():
        raise SystemExit(f"{args.raw} 없음 — k6를 --out json={args.raw.as_posix()}로 실행했는지 확인하세요.")
    points = read_points(args.raw)
    levels = levels_from_points(points)
    if not levels:
        raise SystemExit("요청 기록이 없습니다 — k6가 중간에 중단됐는지 확인하세요.")
    case_id = next((p["data"]["tags"].get("case") for p in points if (p["data"].get("tags") or {}).get("case")), None)
    case = next((c for c in load_cases(args.cases) if c["id"] == case_id), {"id": case_id})

    thresholds = {}
    if args.summary.exists():
        thresholds = thresholds_by_scenario(json.loads(args.summary.read_text(encoding="utf-8")))

    timings = read_events_named(args.log_file, TIMING_EVENT)
    first_start = min(lv["window"][0] for lv in levels.values() if lv["window"][0] is not None)
    started_at = datetime.fromtimestamp(first_start).isoformat(timespec="seconds")
    ordered = sorted(levels.items(), key=lambda kv: kv[1]["window"][0] or 0)
    out_levels = []
    for scenario, level in ordered:
        start, end = level.pop("window")
        in_window = [e for e in timings if start is not None
                     and start - WINDOW_MARGIN_S <= event_ts(e) <= end + WINDOW_MARGIN_S]
        level_thresholds = thresholds.get(scenario, {})
        out_levels.append({
            "scenario": scenario,
            **level,
            "server": server_side_stats(in_window),
            "thresholds": level_thresholds,
            "thresholds_passed": all(level_thresholds.values()) if level_thresholds else None,
        })

    try:
        server_info = httpx.get(f"{args.base_url}/bench/info", timeout=5).json()
    except httpx.HTTPError:
        server_info = {"latency_source": "unknown (서버 종료됨)"}

    model = out_levels[0]["model"]
    path = write_result("load", args.label, {
        "kind": "load",
        "tool": _k6_version(),
        "model": model,
        "label": args.label,
        "started_at": started_at,
        "git_commit": git_commit(),
        "server_info": server_info,
        "case": {k: case[k] for k in ("id", "description", "images", "chunk_counts", "total_chunks") if k in case},
        "levels": out_levels,
    })

    print(f"{'단계':<6}{'요청':>5}{'성공':>5}{'p50':>9}{'p95':>9}{'서버 p50':>10}{'처리량':>11}{'에러':>6}{'dropped':>9}  threshold")
    for lv in out_levels:
        server = lv["server"]["total_p50_s"] if lv["server"] else None
        mark = "–" if lv["thresholds_passed"] is None else "✓" if lv["thresholds_passed"] else "✗"
        print(f"{lv['scenario']:<6}{lv['total_requests']:>5}{lv['success']:>5}{_s(lv['p50_s']):>9}{_s(lv['p95_s']):>9}"
              f"{_s(server):>10}{lv['throughput_rpm']:>8}/min{lv['error_rate']:>6.0%}{lv['dropped_iterations']:>9}  {mark}")
    print(f"\n저장: {path}")


def _s(v: float | None) -> str:
    return "–" if v is None else f"{v:.1f}s"


if __name__ == "__main__":
    main()
