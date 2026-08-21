"""
scripts/accuracy_report.py — 실사용자 피드백(estimate_feedback) 기반 정확도 리포트.

estimate_feedback에 쌓인 실제 계약금액(actual_cost)을, 그 견적이 저장될 당시 산출된 예측 범위
(estimates.result.총_견적_범위)와 비교해서 온라인 정확도를 계산한다.

지표:
  - MAPE (Mean Absolute Percentage Error): |실제 - 예측중간| / 실제. 낮을수록 좋음.
    가견적 시스템에서 흔히 쓰는 지표 — "평균적으로 몇 % 정도 빗나가는가"를 직관적으로 보여준다.
  - Range coverage: 실제 계약금액이 예측 [최소, 최대] 범위 안에 들어온 비율.
    "범위 자체를 얼마나 신뢰할 수 있는가" — MAPE만으론 안 보이는, 범위 폭 설계가 적절한지의 지표.
  - coefficient_version별 breakdown: 계수를 튜닝한 뒤 실제로 정확도가 좋아졌는지/나빠졌는지
    버전 단위로 비교할 수 있게 한다.

cron 등으로 주기적으로 돌려서 운영 정확도 추이를 추적하는 용도. 데이터가 적을 때는(피드백 몇 건 안 됨)
숫자가 크게 흔들릴 수 있으니, N이 충분히 쌓이기 전까지는 참고용으로만 볼 것.

"""

import statistics
import sys
from collections import defaultdict

from bson import ObjectId
from bson.errors import InvalidId
from dotenv import load_dotenv
from pymongo import MongoClient

from app.core.config import get_settings

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")  # Windows 콘솔(cp949)에서 한글/특수문자 출력 깨짐 방지

load_dotenv()


def _fmt_pct(x: float) -> str:
    return f"{x:.1%}"


def main() -> None:
    settings = get_settings()
    db = MongoClient(settings.mongo_uri)[settings.mongo_db_name]

    feedback_docs = list(db["estimate_feedback"].find({}))
    if not feedback_docs:
        print("[INFO] estimate_feedback에 데이터가 없습니다. 아직 집계할 게 없습니다.")
        return

    rows = []
    skipped_missing_estimate = 0

    for fb in feedback_docs:
        try:
            estimate_oid = ObjectId(fb["estimate_id"])
        except InvalidId:
            skipped_missing_estimate += 1
            continue

        estimate = db["estimates"].find_one({"_id": estimate_oid})
        if estimate is None:
            # 견적이 이후 삭제됐거나, feedback만 남고 원본이 없는 경우 — 집계에서 제외
            skipped_missing_estimate += 1
            continue

        범위 = estimate.get("result", {}).get("총_견적_범위")
        if not 범위:
            skipped_missing_estimate += 1
            continue

        actual = fb["actual_cost"]
        lo, mid, hi = 범위["최소"], 범위["중간"], 범위["최대"]

        rows.append({
            "estimate_id": fb["estimate_id"],
            "actual_cost": actual,
            "predicted_lo": lo,
            "predicted_mid": mid,
            "predicted_hi": hi,
            "ape": abs(actual - mid) / actual,
            "in_range": lo <= actual <= hi,
            "coefficient_version": estimate.get("coefficient_version", "unknown"),
            "engine_version": estimate.get("engine_version", "unknown"),
        })

    if not rows:
        print(f"[INFO] 유효한 피드백 {len(feedback_docs)}건 중 매칭되는 견적이 하나도 없습니다 "
              f"(전부 삭제되었거나 estimate_id 불일치). 집계 불가.")
        return

    n = len(rows)
    mape = statistics.mean(r["ape"] for r in rows)
    coverage = sum(r["in_range"] for r in rows) / n

    print(f"전체 피드백: {len(feedback_docs)}건 (매칭 실패/제외 {skipped_missing_estimate}건)")
    print(f"집계 대상: {n}건")
    print()
    print(f"MAPE (평균 오차율): {_fmt_pct(mape)}")
    print(f"Range coverage (실제값이 예측 범위 안에 든 비율): {_fmt_pct(coverage)}")

    if n < 10:
        print("\n[주의] 표본이 10건 미만입니다 — 이 수치는 아직 참고용입니다. 데이터가 더 쌓인 뒤 다시 보세요.")

    by_version: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_version[r["coefficient_version"]].append(r)

    if len(by_version) > 1:
        print("\n--- coefficient_version별 ---")
        for version, version_rows in sorted(by_version.items()):
            v_mape = statistics.mean(r["ape"] for r in version_rows)
            v_coverage = sum(r["in_range"] for r in version_rows) / len(version_rows)
            print(f"  {version} (n={len(version_rows)}): MAPE {_fmt_pct(v_mape)}, "
                  f"coverage {_fmt_pct(v_coverage)}")

    print("\n--- 개별 사례 (오차 큰 순) ---")
    for r in sorted(rows, key=lambda r: r["ape"], reverse=True)[:10]:
        print(f"  {r['estimate_id']}: 실제 {r['actual_cost']:,}원 vs 예측 "
              f"{r['predicted_lo']:,}~{r['predicted_hi']:,}원(중간 {r['predicted_mid']:,}) "
              f"| 오차 {_fmt_pct(r['ape'])} | 범위내={r['in_range']}")


if __name__ == "__main__":
    main()
