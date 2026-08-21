"""
scripts/log_report.py — logs/app.log(구조화 JSON 로그)를 파싱해서 운영 현황 요약을 낸다.

알림(Slack 등)은 아직 안 붙였다 — 대신 이 스크립트를 주기적으로(또는 뭔가 이상하다 싶을 때) 사람이
직접 돌려서 에러율/응답시간/검색 폴백 빈도를 확인하는 방식으로 운영한다.
"""

import argparse
import json
import pathlib
import statistics
import sys
from collections import Counter, defaultdict

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

DEFAULT_LOG_FILE = pathlib.Path("logs/app.log")


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(int(len(s) * p), len(s) - 1)
    return s[idx]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=pathlib.Path, default=DEFAULT_LOG_FILE)
    args = parser.parse_args()

    if not args.file.exists():
        print(f"[INFO] {args.file} 없음 — 아직 로그가 없거나 경로가 다릅니다.")
        return

    events = []
    malformed = 0
    for line in args.file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            malformed += 1

    if not events:
        print("[INFO] 로그가 비어 있습니다.")
        return

    print(f"총 로그 라인: {len(events)}건 (파싱 실패 {malformed}건)")

    level_counts = Counter(e.get("level", "info") for e in events)
    print(f"레벨별: {dict(level_counts)}")

    http = [e for e in events if e.get("event") == "http_request"]
    if http:
        error_count = sum(1 for e in http if e.get("status_code", 200) >= 400)
        rate_limited = sum(1 for e in http if e.get("status_code") == 429)
        print(f"\nHTTP 요청: {len(http)}건, 4xx/5xx {error_count}건 ({error_count / len(http):.1%}), "
              f"rate limited(429) {rate_limited}건")

        by_path: dict[str, list[float]] = defaultdict(list)
        for e in http:
            key = f"{e.get('method')} {e.get('path')}"
            by_path[key].append(e.get("duration_ms", 0))

        print("\n경로별 응답시간(ms):")
        for path, durations in sorted(by_path.items()):
            print(f"  {path}: n={len(durations)}, "
                  f"avg={statistics.mean(durations):.0f}, "
                  f"p95={_percentile(durations, 0.95):.0f}, "
                  f"max={max(durations):.0f}")

    exceptions = [e for e in events if e.get("event") == "unhandled_exception"]
    if exceptions:
        print(f"\n잡히지 않은 예외: {len(exceptions)}건")
        by_type = Counter(e.get("error_type", "Unknown") for e in exceptions)
        for error_type, count in by_type.most_common():
            print(f"  {error_type}: {count}건")

    retrieve = [e for e in events if e.get("event") == "retrieve_cases"]
    if retrieve:
        fallback_count = sum(1 for e in retrieve if e.get("fallback"))
        print(f"\nretrieve_cases: {len(retrieve)}건, "
              f"Stage 5(필터 없음) 폴백 {fallback_count}건 ({fallback_count / len(retrieve):.1%})")
        stage_counts = Counter(e.get("stage") for e in retrieve)
        print(f"  Stage별 분포: {dict(sorted(stage_counts.items()))}")

    no_match = [e for e in events if e.get("event") == "generate_no_match"]
    if no_match:
        print(f"\n사례 없음(422 처리): {len(no_match)}건")


if __name__ == "__main__":
    main()
