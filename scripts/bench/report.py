"""
scripts/bench/report.py — bench_latency / bench_load 결과 JSON을 모아 HTML 리포트 한 장으로 만든다.

외부 라이브러리·CDN 없이 인라인 SVG로 그리는 단일 파일이라, 브라우저로 바로 열거나 그대로
공유할 수 있다. 결과 파일을 넘긴 순서가 곧 비교 순서다 (첫 번째 = 기준, 예: baseline).

사용법:
  python -m scripts.bench.report logs/bench/latency_baseline_*.json logs/bench/load_baseline_*.json
  python -m scripts.bench.report logs/bench/latency_baseline_*.json logs/bench/latency_parallel_*.json \\
      --out logs/bench/report_parallel.html
"""

import argparse
import glob
import json
import pathlib
import sys

from scripts.bench.common import BENCH_DIR

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

TEMPLATE = pathlib.Path(__file__).with_name("report_template.html")
PLACEHOLDER = '"__BENCH_DATA__"'


def build_report(result_paths: list[pathlib.Path]) -> str:
    data: dict[str, list] = {"latency": [], "load": []}
    for path in result_paths:
        result = json.loads(path.read_text(encoding="utf-8"))
        data[result["kind"]].append(result)
    # </script> 조기 종료 방지 — JSON 안의 "</"를 이스케이프
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", help="결과 JSON 경로 (glob 가능, 넘긴 순서 = 비교 순서)")
    parser.add_argument("--out", type=pathlib.Path, default=BENCH_DIR / "report.html")
    args = parser.parse_args()

    paths: list[pathlib.Path] = []
    for pattern in args.results:
        matched = sorted(glob.glob(pattern)) or [pattern]
        paths.extend(pathlib.Path(p) for p in matched)
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise SystemExit(f"결과 파일 없음: {missing}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(build_report(paths), encoding="utf-8")
    print(f"리포트: {args.out.resolve()}  (결과 {len(paths)}개)")


if __name__ == "__main__":
    main()
