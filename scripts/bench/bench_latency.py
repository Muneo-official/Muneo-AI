"""
scripts/bench/bench_latency.py — 단건 응답시간·비용 측정 (실제 Vision API 호출, 비용 발생).

bench 서버(scripts/bench/server.py, real 모드)에 케이스별로 실제 multipart 요청을 N회 보내고,
  - 클라이언트: 요청 전송~응답 수신 wall time (사용자가 실제로 기다리는 시간)
  - 서버: 응답 헤더 X-Request-Id로 logs/app.log의 구간 시간·Vision 호출 타임라인·토큰
을 묶어서 logs/bench/latency_<label>_<시각>.json에 저장한다. 케이스별 첫 성공 응답은
정확도 비교용 스냅샷(result_signature)으로 같이 남긴다.

사용법:
  python -m scripts.bench.bench_latency --label baseline
  python -m scripts.bench.bench_latency --label parallel --runs 3 --only S3,S4
"""

import argparse
import pathlib
import statistics
import sys
import time
from datetime import datetime

import httpx

from pipeline.vision_client import MODEL
from scripts.bench.common import (
    DEFAULT_CASES_FILE,
    DEFAULT_LOG_FILE,
    PRICING_CHECKED_AT,
    build_server_record,
    cost_usd,
    form_fields,
    git_commit,
    load_cases,
    log_size,
    percentile,
    read_events,
    result_signature,
    upload_files,
    write_result,
)

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

ENDPOINT = "/risk-detector/analyze"
REQUEST_TIMEOUT_S = 900


def _post(client: httpx.Client, case: dict) -> dict:
    started = time.perf_counter()
    try:
        resp = client.post(ENDPOINT, data=form_fields(case), files=upload_files(case))
    except httpx.HTTPError as e:
        return {"status": None, "wall_s": round(time.perf_counter() - started, 3), "error": repr(e)}
    wall_s = round(time.perf_counter() - started, 3)
    out = {"status": resp.status_code, "wall_s": wall_s, "request_id": resp.headers.get("x-request-id")}
    if resp.status_code == 200:
        out["body"] = resp.json()
    else:
        out["error"] = resp.text[:500]
    return out


def _confirm(cases: list[dict], runs: int, warmup: int) -> bool:
    calls = sum(c["total_chunks"] for c in cases) * runs + (cases[0]["total_chunks"] * warmup)
    print(f"모델 {MODEL} · 케이스 {len(cases)}개 × {runs}회 + 워밍업 {warmup}회")
    print(f"예상 Vision 호출 약 {calls}회 (실제 API 과금). 계속할까요? [y/N] ", end="", flush=True)
    return input().strip().lower() == "y"


def _summarize(requests: list[dict], cases: list[dict]) -> None:
    print(f"\n{'case':<5}{'chunks':>7}{'p50 wall':>10}{'max':>8}{'parse':>8}{'rule':>7}{'price':>7}"
          f"{'Σvision':>9}{'in tok':>9}{'out tok':>9}{'cost $':>9}")
    for case in cases:
        rows = [r for r in requests if r["case_id"] == case["id"] and not r["warmup"] and r.get("server")]
        if not rows:
            print(f"{case['id']:<5}  (성공한 요청 없음)")
            continue

        def med(key: str) -> float:
            return statistics.median(r["server"][key] for r in rows)

        walls = [r["wall_s"] for r in rows]
        print(
            f"{case['id']:<5}{case['total_chunks']:>7}{percentile(walls, 50):>10.1f}{max(walls):>8.1f}"
            f"{med('parse_images_s'):>8.1f}{med('rule_analyze_s'):>7.2f}{med('price_check_s'):>7.2f}"
            f"{med('vision_latency_sum_s'):>9.1f}{med('input_tokens'):>9.0f}{med('output_tokens'):>9.0f}"
            f"{statistics.median(r['cost_usd'] for r in rows):>9.4f}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True, help="이 측정의 이름 (예: baseline, parallel)")
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--only", default="", help="측정할 케이스 id (쉼표 구분)")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--warmup", type=int, default=1, help="첫 케이스로 보내고 버리는 요청 수")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--log-file", type=pathlib.Path, default=DEFAULT_LOG_FILE)
    parser.add_argument("--yes", action="store_true", help="비용 확인 프롬프트 생략")
    args = parser.parse_args()

    cases = load_cases(args.cases)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c["id"] in wanted]
    if not cases:
        raise SystemExit("측정할 케이스가 없습니다 — select_cases를 먼저 실행하세요.")

    with httpx.Client(base_url=args.base_url, timeout=REQUEST_TIMEOUT_S) as client:
        info = client.get("/bench/info").json()
        if info.get("vision") != "real":
            raise SystemExit(f"서버가 real 모드가 아닙니다: {info} — mock 결과로 비용·지연을 재면 안 됩니다.")
        if not args.yes and not _confirm(cases, args.runs, args.warmup):
            return

        offset = log_size(args.log_file)
        started_at = datetime.now().isoformat(timespec="seconds")
        requests: list[dict] = []
        plan = [(cases[0], True, i) for i in range(args.warmup)]
        plan += [(case, False, i) for case in cases for i in range(args.runs)]
        for n, (case, warmup, run_index) in enumerate(plan, 1):
            tag = "warmup" if warmup else f"run {run_index + 1}/{args.runs}"
            print(f"[{n}/{len(plan)}] {case['id']} {tag} ...", end=" ", flush=True)
            result = _post(client, case)
            print(f"{result['status']} {result['wall_s']:.1f}s")
            requests.append({"case_id": case["id"], "warmup": warmup, "run_index": run_index, **result})

    events = read_events(args.log_file, {r["request_id"] for r in requests if r.get("request_id")}, offset)
    snapshots: dict[str, dict] = {}
    for r in requests:
        body = r.pop("body", None)
        server = build_server_record(events.get(r.get("request_id"), [])) if r.get("request_id") else None
        r["server"] = server
        if server:
            r["cost_usd"] = round(cost_usd(
                MODEL, server["input_tokens"], server["output_tokens"],
                server["cache_creation_input_tokens"], server["cache_read_input_tokens"],
            ), 6)
        if body and not r["warmup"]:
            # 실행마다 남긴다 — 같은 입력이어도 모델 출력이 실행마다 조금씩 달라서(베이스라인 S1 항목 수 28·30·28),
            # 개선 전후 비교는 "첫 실행과 같은가"가 아니라 "실행 간 변동 범위 안인가"로 봐야 한다
            r["signature"] = {"line_item_count": server and server["line_item_count"], **result_signature(body)}
            snapshots.setdefault(r["case_id"], r["signature"])

    missing = [r["request_id"] for r in requests if r["status"] == 200 and not r["server"]]
    if missing:
        print(f"[WARN] 서버 로그를 못 찾은 요청 {len(missing)}건 — --log-file 경로가 서버와 같은지 확인하세요.")

    path = write_result("latency", args.label, {
        "kind": "latency",
        "label": args.label,
        "started_at": started_at,
        "git_commit": git_commit(),
        "model": MODEL,
        "pricing_checked_at": PRICING_CHECKED_AT,
        "base_url": args.base_url,
        "server_info": info,
        "runs": args.runs,
        "cases": [{k: c[k] for k in ("id", "description", "article_id", "images", "chunk_counts", "total_chunks")}
                  for c in cases],
        "requests": requests,
        "snapshots": snapshots,
    })
    _summarize(requests, cases)
    print(f"\n저장: {path}")


if __name__ == "__main__":
    main()
