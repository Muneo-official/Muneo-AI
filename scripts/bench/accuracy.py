"""
scripts/bench/accuracy.py — 기준 측정 대비 후보 측정의 파싱 정확도 판정 (docs/RISK_DETECTOR_COST_LOG.md 3장).

같은 입력이어도 모델 출력이 실행마다 달라서(베이스라인 항목 수 S1 28·30·28) "첫 실행과 같은가"로는 판정할 수 없다.
그래서 지표마다 **기준 실행들의 min~max를 허용폭만큼 넓힌 범위** 안에 후보 실행들의 중앙값이 들어오는지 본다.
허용폭은 기준 중앙값 기준이다 — 기준 변동이 0인 케이스(S3 항목 수 165·165·165)도 판정할 수 있게.

| 지표 | 허용폭 |
|---|---|
| 항목 수 | ±3% |
| 공종별 금액 합계 | ±5% (공종마다) |
| 공종 분포 (금액 비중) | ±3%p (공종마다) |
| 전체 금액 합계 / total_cost | ±2% |
| 리스크 이슈 수 | 판정 안 함, 기록만 (기준에서도 S1 1↔4개로 흔들림) |

공종별 지표는 bench 서버의 파싱 결과 캡처가 있는 측정에만 있다. 기준 쪽에 없으면 그 지표는 "판정 불가"로 둔다.

사용법:
  python -m scripts.bench.accuracy logs/bench/latency_base_*.json logs/bench/latency_b2_*.json
"""

import argparse
import json
import pathlib
import statistics
import sys

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

ITEM_COUNT_TOL = 0.03
CATEGORY_AMOUNT_TOL = 0.05
CATEGORY_SHARE_TOL_PP = 3.0
TOTAL_TOL = 0.02


def run_signatures(result: dict, case_id: str) -> list[dict]:
    """실행별 지표. 항목 수는 서버 로그 값을 쓴다 — 실행별 signature가 없는 초기 측정(베이스라인)에도 있어서."""
    return [
        {**(r.get("signature") or {}), "line_item_count": r["server"]["line_item_count"]}
        for r in result["requests"]
        if r["case_id"] == case_id and not r["warmup"] and r.get("server")
    ]


def _allowed(base: list[float], rel: float | None = None, abs_: float | None = None) -> tuple[float, float]:
    mid = statistics.median(base)
    tol = mid * rel if rel is not None else (abs_ or 0)
    return min(min(base), mid - tol), max(max(base), mid + tol)


def _row(metric: str, base: list[float], cand: list[float], **tol) -> dict:
    row = {"metric": metric, "base": base, "candidate": cand, "allowed": None, "candidate_median": None, "ok": None}
    if not base or not cand:
        row["note"] = "기준에 지표 없음" if not base else "후보에 지표 없음"
        return row
    lo, hi = _allowed(base, **tol)
    m = statistics.median(cand)
    row.update(allowed=[lo, hi], candidate_median=m, ok=lo <= m <= hi)
    return row


def _values(sigs: list[dict], key: str) -> list[float]:
    return [s[key] for s in sigs if s.get(key) is not None]


def _shares(sig: dict) -> dict[str, float]:
    total = sig.get("amount_sum") or 0
    return {c: 100 * a / total for c, a in sig["category_amounts"].items()} if total else {}


def judge_case(base_sigs: list[dict], cand_sigs: list[dict]) -> list[dict]:
    rows = [
        _row("항목 수", _values(base_sigs, "line_item_count"), _values(cand_sigs, "line_item_count"), rel=ITEM_COUNT_TOL),
        _row("전체 금액 합계", _values(base_sigs, "amount_sum"), _values(cand_sigs, "amount_sum"), rel=TOTAL_TOL),
        _row("total_cost", _values(base_sigs, "total_cost"), _values(cand_sigs, "total_cost"), rel=TOTAL_TOL),
    ]

    base_cat = [s for s in base_sigs if "category_amounts" in s]
    cand_cat = [s for s in cand_sigs if "category_amounts" in s]
    if base_cat and cand_cat:
        # 어떤 실행에서만 나온 공종은 나머지 실행에서 0원으로 본다 — 공종이 나타나거나 사라지는 것도 변화다
        categories = sorted({c for s in base_cat + cand_cat for c in s["category_amounts"]})
        for c in categories:
            rows.append(_row(f"금액 · {c}", [s["category_amounts"].get(c, 0) for s in base_cat],
                             [s["category_amounts"].get(c, 0) for s in cand_cat], rel=CATEGORY_AMOUNT_TOL))
        for c in categories:
            rows.append(_row(f"비중 · {c} (%)", [_shares(s).get(c, 0.0) for s in base_cat],
                             [_shares(s).get(c, 0.0) for s in cand_cat], abs_=CATEGORY_SHARE_TOL_PP))

    risk = _row("리스크 이슈 수 (기록만)", _values(base_sigs, "total_risk_items"), _values(cand_sigs, "total_risk_items"))
    risk.update(allowed=None, ok=None)
    rows.append(risk)
    return rows


def judge(base: dict, cand: dict) -> dict:
    """케이스별 판정. 케이스는 판정 대상 지표가 하나라도 범위 밖이면 불합격, 판정 가능한 지표가 없으면 None."""
    cases = {}
    for case in base["cases"]:
        rows = judge_case(run_signatures(base, case["id"]), run_signatures(cand, case["id"]))
        verdicts = [r["ok"] for r in rows if r["ok"] is not None]
        cases[case["id"]] = {"passed": all(verdicts) if verdicts else None, "rows": rows}
    return {"base_label": base["label"], "candidate_label": cand["label"], "cases": cases}


def _fmt(v: float | None) -> str:
    if v is None:
        return "–"
    return f"{v:,.1f}" if isinstance(v, float) and abs(v) < 1000 else f"{v:,.0f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base", type=pathlib.Path, help="기준 bench_latency 결과 JSON")
    parser.add_argument("candidate", type=pathlib.Path, help="후보 bench_latency 결과 JSON")
    parser.add_argument("--all", action="store_true", help="통과한 지표도 모두 출력")
    args = parser.parse_args()

    result = judge(*(json.loads(p.read_text(encoding="utf-8")) for p in (args.base, args.candidate)))
    print(f"기준 {result['base_label']} → 후보 {result['candidate_label']}")
    failed = False
    for case_id, case in result["cases"].items():
        mark = {True: "✓ 통과", False: "✗ 불합격", None: "– 판정 불가"}[case["passed"]]
        failed |= case["passed"] is False
        print(f"\n[{case_id}] {mark}")
        for r in case["rows"]:
            if not args.all and r["ok"] is True:
                continue
            allowed = f"{_fmt(r['allowed'][0])}~{_fmt(r['allowed'][1])}" if r["allowed"] else r.get("note", "")
            ok = {True: "✓", False: "✗", None: "·"}[r["ok"]]
            print(f"  {ok} {r['metric']:<24} 기준 {' · '.join(_fmt(v) for v in r['base']) or '–':<30} "
                  f"후보 {' · '.join(_fmt(v) for v in r['candidate']) or '–':<30} 허용 {allowed}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
