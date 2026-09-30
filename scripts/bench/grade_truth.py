"""
scripts/bench/grade_truth.py — bench_latency 결과를 정답 파일로 채점해 모델(또는 방식)끼리 비교한다. API 호출 없음.

accuracy.py("기준 측정과 같은가")는 기준의 오분류까지 정답으로 취급한다. 모델을 바꾸면 경계 공종 분류가
반드시 흔들리므로(pipeline/results/risk_detector_cost_optimization.md), 여기서는 사람이 확정한 정답으로 채점한다.

지표 (실행마다 계산해 결과 파일별로 모은다)
- 정답 채점: logs/bench/category_truth.json의 항목(분류가 흔들리던 항목)을 최종 항목에서 (케이스, code, 금액)으로
  찾아 공종이 정답과 같은지. 못 찾은 항목은 오답으로 세고 따로 표시한다(행 누락·금액 오독).
  `disputed`가 붙은 항목(공종 정의가 코드베이스 안에서 엇갈리는 폐기물처리·도어)은 기본으로 채점하지 않는다.
- S1 정답 일치: 항목 28개·금액 합계 7,386,000원 (이미지와 한 줄씩 대조해 확정한 값)
- 필름 분류: 필름시공 항목(S3, code 1001, 3,500,000원)이 필름으로 분류됐는지 + 리포트에 필름 이슈가 있는지
- 도구 미호출: 강제 도구 호출이 안 되는 모델에서 재시도 후에도 도구 대신 글로 답한 Vision 호출 수
- 재시도: 첫 호출이 도구 대신 글로 답해 한 번 더 부른 Vision 호출 수 (재시도가 성공하면 도구 미호출엔 안 잡힌다)
- 요청당 비용·응답시간 (중앙값)

사용법:
  python -m scripts.bench.grade_truth logs/bench/latency_cost_b2_film_*.json logs/bench/latency_model-haiku_*.json
"""

import argparse
import glob
import json
import pathlib
import statistics
import sys
from collections import defaultdict

from pipeline.parsing import _safe_int

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

DEFAULT_TRUTH = pathlib.Path("logs/bench/category_truth.json")
S1_EXPECTED = {"line_item_count": 28, "amount_sum": 7_386_000}
FILM_KEY = ("S3", "1001", 3_500_000)


def _key(item: dict) -> tuple[str, int]:
    # 모델이 금액을 "3,500,000"·"약 350만"처럼 내도 채점 전체가 멈추지 않게 파싱 단계와 같은 변환을 쓴다
    return str(item.get("code", "")).strip(), _safe_int(item.get("amount"))


def grade_run(case_id: str, line_items: list[dict], truth: list[dict]) -> dict:
    """한 실행의 최종 항목을 그 케이스의 정답 항목으로 채점한다."""
    pool: dict[tuple[str, int], list[str]] = defaultdict(list)
    for item in line_items:
        pool[_key(item)].append(item.get("category", ""))
    correct = missing = 0
    wrong: list[str] = []
    for t in truth:
        found = pool.get((str(t["code"]), int(t["amount"])))
        if not found:
            missing += 1
            continue
        predicted = found.pop(0)  # 같은 code·금액 행이 둘이면 하나씩 소비
        if predicted == t["truth"]:
            correct += 1
        else:
            wrong.append(f"{t['code']} {t['truth']}→{predicted}")
    return {"total": len(truth), "correct": correct, "missing": missing, "wrong": wrong}


def _film(line_items: list[dict]) -> str | None:
    for item in line_items:
        if _key(item) == FILM_KEY[1:]:
            return item.get("category")
    return None


def grade_file(path: pathlib.Path, truth: list[dict]) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    truth_by_case: dict[str, list[dict]] = defaultdict(list)
    for t in truth:
        truth_by_case[t["case"]].append(t)

    runs = []
    for r in data["requests"]:
        if r.get("warmup") or r.get("status") != 200 or not r.get("parsed"):
            continue
        items = r["parsed"]["line_items"]
        sig = r.get("signature") or {}
        row = {
            "case": r["case_id"],
            "wall_s": r["wall_s"],
            "cost_usd": r.get("cost_usd"),
            "grade": grade_run(r["case_id"], items, truth_by_case.get(r["case_id"], [])),
            "retried": sum(1 for c in (r.get("server") or {}).get("vision_calls", []) if c.get("retried")),
            "tool_not_called": sum(1 for c in (r.get("server") or {}).get("vision_calls", [])
                                   if c.get("tool_called") is False),
        }
        if r["case_id"] == "S1":
            row["s1_exact"] = all(sig.get(k) == v for k, v in S1_EXPECTED.items())
        if r["case_id"] == FILM_KEY[0]:
            row["film_category"] = _film(items)
            row["film_issue"] = any(i[0] == "필름" for i in sig.get("issues", []))
        runs.append(row)
    return {"path": str(path), "label": data.get("label"), "model": data.get("model"), "runs": runs}


def summarize(graded: dict) -> dict:
    runs = graded["runs"]
    graded_runs = [r for r in runs if r["grade"]["total"]]
    total = sum(r["grade"]["total"] for r in graded_runs)
    out = {
        "label": graded["label"],
        "model": graded["model"],
        "truth_accuracy": sum(r["grade"]["correct"] for r in graded_runs) / total if total else None,
        "truth_missing": sum(r["grade"]["missing"] for r in graded_runs),
        "truth_items": total,
        "tool_not_called": sum(r["tool_not_called"] for r in runs),
        "retried": sum(r["retried"] for r in runs),
    }
    s1 = [r["s1_exact"] for r in runs if "s1_exact" in r]
    out["s1_exact"] = f"{sum(s1)}/{len(s1)}" if s1 else "-"
    film = [r["film_category"] == "필름" for r in runs if "film_category" in r]
    out["film_ok"] = f"{sum(film)}/{len(film)}" if film else "-"
    film_issue = [r["film_issue"] for r in runs if "film_issue" in r]
    out["film_issue"] = f"{sum(film_issue)}/{len(film_issue)}" if film_issue else "-"
    by_case: dict[str, list[dict]] = defaultdict(list)
    for r in runs:
        by_case[r["case"]].append(r)
    out["cases"] = {
        c: {
            "n": len(rs),
            # 결과 파일마다 담긴 케이스가 달라 전체 정답률끼리는 비교가 안 된다 — 케이스별로도 본다
            "truth_accuracy": sum(r["grade"]["correct"] for r in rs) / sum(r["grade"]["total"] for r in rs)
            if sum(r["grade"]["total"] for r in rs) else None,
            "wall_p50": statistics.median(r["wall_s"] for r in rs),
            "cost_p50": statistics.median(r["cost_usd"] for r in rs if r["cost_usd"] is not None)
            if any(r["cost_usd"] is not None for r in rs) else None,
        }
        for c, rs in sorted(by_case.items())
    }
    wrong: dict[str, int] = defaultdict(int)
    for r in graded_runs:
        for w in r["grade"]["wrong"]:
            wrong[f"{r['case']} {w}"] += 1
    out["wrong"] = dict(sorted(wrong.items(), key=lambda kv: -kv[1]))
    return out


def _print(summaries: list[dict]) -> None:
    print(f"\n{'측정':<28}{'모델':<20}{'정답률':>8}{'누락':>6}{'S1일치':>8}{'필름':>6}{'필름이슈':>8}{'도구X':>6}{'재시도':>6}")
    for s in summaries:
        acc = f"{s['truth_accuracy'] * 100:.0f}%" if s["truth_accuracy"] is not None else "-"
        print(f"{(s['label'] or '')[:27]:<28}{(s['model'] or '')[:19]:<20}{acc:>8}{s['truth_missing']:>6}"
              f"{s['s1_exact']:>8}{s['film_ok']:>6}{s['film_issue']:>8}{s['tool_not_called']:>6}{s['retried']:>6}")
    print(f"\n{'측정':<28}{'케이스':<6}{'n':>3}{'정답률':>8}{'wall p50':>10}{'비용 p50':>10}")
    for s in summaries:
        for c, v in s["cases"].items():
            cost = f"${v['cost_p50']:.4f}" if v["cost_p50"] is not None else "-"
            acc = f"{v['truth_accuracy'] * 100:.0f}%" if v["truth_accuracy"] is not None else "-"
            print(f"{(s['label'] or '')[:27]:<28}{c:<6}{v['n']:>3}{acc:>8}{v['wall_p50']:>9.1f}s{cost:>10}")
    for s in summaries:
        if s["wrong"]:
            top = ", ".join(f"{k}({n})" for k, n in list(s["wrong"].items())[:8])
            print(f"\n[{s['label']}] 오답 (항목·횟수): {top}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", nargs="+", help="bench_latency 결과 JSON (glob 가능)")
    parser.add_argument("--truth", type=pathlib.Path, default=DEFAULT_TRUTH)
    parser.add_argument("--json", type=pathlib.Path, help="요약을 JSON으로도 저장")
    parser.add_argument("--include-disputed", action="store_true",
                        help="정의 논쟁 항목(정답 파일의 disputed)도 채점 (기본 제외)")
    parser.add_argument("--by-model", action="store_true",
                        help="결과 파일을 모델별로 합쳐 채점 (케이스별로 나눠 잰 측정·스모크를 한 모델로 묶을 때)")
    args = parser.parse_args()

    truth = json.loads(args.truth.read_text(encoding="utf-8"))
    if not args.include_disputed:
        # 정의가 코드베이스 안에서 엇갈리는 항목(disputed)은 어느 쪽을 정답으로 두느냐가 곧 승패라 기본 제외
        skipped = [t for t in truth if t.get("disputed")]
        truth = [t for t in truth if not t.get("disputed")]
        if skipped:
            print(f"정의 논쟁 항목 {len(skipped)}개 채점 제외: " + ", ".join(f"{t['case']} {t['code']}" for t in skipped))
    paths = [pathlib.Path(p) for pattern in args.results for p in (sorted(glob.glob(pattern)) or [pattern])]
    graded = [grade_file(p, truth) for p in paths]
    if args.by_model:
        merged: dict[str, dict] = {}
        for g in graded:
            m = merged.setdefault(g["model"], {"path": "", "label": g["model"], "model": g["model"], "runs": []})
            m["runs"].extend(g["runs"])
        graded = list(merged.values())
    summaries = [summarize(g) for g in graded]
    _print(summaries)
    if args.json:
        args.json.write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n저장: {args.json}")


if __name__ == "__main__":
    main()
