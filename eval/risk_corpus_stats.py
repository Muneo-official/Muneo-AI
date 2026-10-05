"""
eval/risk_corpus_stats.py — 실제 견적서에 리스크 진단 규칙을 적용했을 때 지적이 얼마나 나오는지 센다.

    python -m eval.risk_corpus_stats            # 요약
    python -m eval.risk_corpus_stats --examples 5   # 지적 종류마다 예시 5개

코퍼스(estimate_cases)에 이미 파싱돼 있는 서울·수도권 견적서의 품목에 규칙(RiskAnalyzer)만 적용한다. Vision도 가격
비교도 부르지 않아 비용이 들지 않는다. 결함을 심은 벤치마크(eval/risk_benchmark.py)가 "심은 결함을 찾는가"를
본다면, 이 스크립트는 "평범한 견적서에 지적을 얼마나 하는가"를 본다 — 찾은 비율이 올라도 여기서 지적 수가
같이 늘면 개선이 아니다. 거의 모든 견적서에 뜨는 지적은 경고 구실을 못 한다.

가격 이상 지적은 세지 않는다. 사례 검색이 필요하고, 견적서 자신이 코퍼스에 있어 자기 자신과 비교하게 된다.
"""

import argparse
import statistics
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv
from pymongo import MongoClient

from app.core.config import get_settings
from app.domain.risk_analyzer import RiskAnalyzer

load_dotenv()

REGIONS = ["서울", "경기", "인천"]


def load_quotes() -> list[list[dict]]:
    """서울·수도권 주거 견적서의 품목 목록들."""
    settings = get_settings()
    collection = MongoClient(settings.mongo_uri)[settings.mongo_db_name]["estimate_cases"]
    docs = collection.find({"region": {"$in": REGIONS}, "is_non_residential": {"$ne": True}}, {"parsed_estimate": 1})
    return [items for d in docs if (items := (d.get("parsed_estimate") or {}).get("line_items"))]


def collect(quotes: list[list[dict]], analyzer: RiskAnalyzer | None = None) -> dict:
    """견적서마다 규칙을 적용해 지적을 모은다. 반환: 견적서별 종류 개수, 지적 제목별 (개수, 걸린 견적서 수, 예시)."""
    analyzer = analyzer or RiskAnalyzer()
    per_quote: list[Counter] = []
    by_title: dict[tuple[str, str], dict] = defaultdict(lambda: {"count": 0, "quotes": 0, "examples": []})
    for items in quotes:
        issues, _ = analyzer.analyze(items)
        per_quote.append(Counter(issue.type for issue in issues))
        for key in {(issue.type, issue.title) for issue in issues}:
            by_title[key]["quotes"] += 1
        for issue in issues:
            entry = by_title[(issue.type, issue.title)]
            entry["count"] += 1
            if len(entry["examples"]) < 50:
                entry["examples"].append(issue.detail)
    return {"per_quote": per_quote, "by_title": dict(by_title)}


def summarize(stats: dict) -> dict:
    per_quote = stats["per_quote"]
    totals = [sum(c.values()) for c in per_quote]
    n = len(per_quote)
    return {
        "견적서": n,
        "견적서당_지적_중앙값": statistics.median(totals) if totals else 0,
        "견적서당_지적_평균": statistics.mean(totals) if totals else 0,
        "지적_없는_견적서": sum(t == 0 for t in totals),
        "종류별_걸린_견적서_비율": {kind: sum(c[kind] > 0 for c in per_quote) / n for kind in sorted({k for c in per_quote for k in c})} if n else {},
    }


def print_report(stats: dict, examples: int) -> None:
    s = summarize(stats)
    n = s["견적서"]
    print(f"견적서 {n}건 — 견적서당 지적 중앙값 {s['견적서당_지적_중앙값']:g}개, 평균 {s['견적서당_지적_평균']:.1f}개, "
          f"지적이 없는 견적서 {s['지적_없는_견적서']}건")
    print("종류별로 한 번이라도 지적받은 견적서: " + ", ".join(f"{k} {v:.0%}" for k, v in s["종류별_걸린_견적서_비율"].items()))
    print("\n지적 제목별 (지적 수 / 걸린 견적서 비율)")
    for (kind, title), entry in sorted(stats["by_title"].items(), key=lambda kv: -kv[1]["count"]):
        print(f"  [{kind}] {title}: {entry['count']}개 / {entry['quotes'] / n:.0%}")
        for detail, count in Counter(entry["examples"]).most_common(examples):
            print(f"      {detail}" + (f"  ×{count}" if count > 1 else ""))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--examples", type=int, default=0, help="지적 제목마다 보여 줄 예시 수")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    print_report(collect(load_quotes()), args.examples)


if __name__ == "__main__":
    main()
