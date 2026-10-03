"""
eval/quote_benchmark.py — 가견적 채점: 정답셋의 입력으로 엔진을 돌려 실제 견적서 금액과 비교한다.

    python -m eval.quote_benchmark                  # 개선용(dev) 세트 채점
    python -m eval.quote_benchmark --split eval --final   # 평가용 세트 — 마지막 비교 때 한 번만

`/estimates/generate`가 부르는 EstimateEngine.generate()를 같은 설정(리랭커 사용 여부, 후보 풀, 활성 보정계수)으로
직접 호출한다. 정답으로 뽑은 견적은 코퍼스에 그대로 남아 있으므로, 채점할 때는 같은 의뢰(request_url)의 사례를
검색 결과에서 뺀다 — 안 빼면 엔진이 자기 답을 참고 사례로 쓴다. 같은 의뢰에 여러 업체 견적이 달리므로 게시글이
아니라 의뢰 단위로 뺀다.

보는 값
  - 총액 오차율: (엔진 중간값 − 정답) / 정답. 절대값의 중앙값과 과대·과소 건수
  - 범위 적중률: 정답이 [최소, 최대] 안에 든 비율. 범위를 넓게 부르면 올라가므로 폭((최대−최소)/중간)과 함께 본다
  - 총액은 간접비 포함 기준과 직접비 기준 두 가지로 채점한다 — 코퍼스의 total_cost에 간접비 포함 여부가 섞여 있다
  - 전체 시공과 부분 시공은 총액 산출 경로가 달라 나눠서 보고, 플래그가 붙은 건을 뺀 값도 함께 낸다
  - 구간은 정답 레코드 단위 부트스트랩 95%

건별 결과는 커밋하지 않는 위치(estimate_data/_gt_review/benchmark_runs/)에 저장한다.
"""

import argparse
import asyncio
import datetime
import json
import random
import statistics

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine
from app.repositories.case_repository import CaseRepository
from app.repositories.coefficient_repository import CoefficientRepository
from eval.quote_ground_truth import GROUND_TRUTH_PATH, REVIEW_DIR, 공종_순서

load_dotenv()

TARGET_HIT_RATE = 0.80
RUNS_DIR = REVIEW_DIR / "benchmark_runs"
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 68


class LeaveOutCaseRepository(CaseRepository):
    """채점 중인 정답 견적과 같은 의뢰의 사례를 검색 결과에서 빼는 CaseRepository.

    서비스 코드는 건드리지 않고 엔진에 주입하는 저장소만 바꾼다. 뺀 만큼 후보 풀이 줄지 않도록
    그 의뢰에 달린 사례 수만큼 더 가져온 뒤 잘라낸다.
    """

    def __init__(self, collection, settings):
        super().__init__(collection, settings)
        self._left_out_url: str | None = None
        self._left_out_article: str | None = None
        self._extra = 0

    async def leave_out(self, request_url: str | None, article_id: str | None) -> int:
        """이후 검색에서 뺄 의뢰를 정한다. 반환: 코퍼스에서 빠지는 사례 수."""
        self._left_out_url = request_url or None
        self._left_out_article = str(article_id) if article_id else None
        conds = []
        if self._left_out_url:
            conds.append({"request_url": self._left_out_url})
        if self._left_out_article:
            conds.append({"article_id": self._left_out_article})
        self._extra = await self._collection.count_documents({"$or": conds}) if conds else 0
        return self._extra

    def _is_left_out(self, case: dict) -> bool:
        if self._left_out_url and case.get("request_url") == self._left_out_url:
            return True
        return bool(self._left_out_article) and str(case.get("article_id")) == self._left_out_article

    async def vector_search(self, query_embedding, mongo_filter, limit, num_candidates=150):
        cases = await super().vector_search(query_embedding, mongo_filter, limit + self._extra, num_candidates)
        return [c for c in cases if not self._is_left_out(c)][:limit]


# ── 채점 (순수 계산) ──────────────────────────────────────────────────────


def score_range(rng: dict | None, truth: int) -> dict | None:
    """범위 하나를 정답 금액 하나와 비교한다. 범위가 없거나 정답이 0 이하면 None."""
    if not rng or truth <= 0:
        return None
    lo, mid, hi = rng["최소"], rng["중간"], rng["최대"]
    return {
        "오차율": (mid - truth) / truth,
        "적중": lo <= truth <= hi,
        "폭": (hi - lo) / mid if mid > 0 else None,
    }


def score_record(record: dict, output: dict) -> dict:
    """정답 레코드 하나와 엔진 출력 하나를 채점한다. 엔진이 견적을 못 냈으면 실패로 남긴다."""
    row = {
        "id": record["id"],
        "시공범위": record["input"]["시공범위"],
        "flags": list(record["flags"]),
    }
    if "error" in output:
        return {**row, "실패": output["error"]}

    truth = record["truth"]
    총범위 = output["총_견적_범위"]
    공종별_범위 = output["공종별_단가_범위"]
    return {
        **row,
        "참고_사례_수": output["참고_사례_수"],
        "총액": score_range(총범위, truth["비교_총액"]),
        "직접비": score_range(총범위, truth["비교_직접비"]),
        # 정답에 있는 공종인데 엔진이 금액을 못 낸 경우는 None으로 남겨 미산출로 센다
        "공종별": {g: score_range(공종별_범위.get(g), amount) for g, amount in truth["공종별"].items()},
    }


def bootstrap_ci(values: list[float], stat, n: int = BOOTSTRAP_N, seed: int = BOOTSTRAP_SEED) -> tuple[float, float] | None:
    """값 목록을 복원추출해 통계량의 95% 구간을 구한다. 값이 2개 미만이면 None."""
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    stats = sorted(stat(rng.choices(values, k=len(values))) for _ in range(n))
    return stats[int(n * 0.025)], stats[min(int(n * 0.975), n - 1)]


def summarize_scores(scores: list[dict | None]) -> dict:
    """score_range() 결과 여러 개를 한 줄 요약으로. None(미산출)은 건수만 세고 지표에서 뺀다."""
    scored = [s for s in scores if s is not None]
    summary = {"건수": len(scored), "미산출": len(scores) - len(scored)}
    if not scored:
        return summary
    abs_err = [abs(s["오차율"]) for s in scored]
    hits = [1.0 if s["적중"] else 0.0 for s in scored]
    widths = [s["폭"] for s in scored if s["폭"] is not None]
    summary.update({
        "절대오차율_중앙값": statistics.median(abs_err),
        "절대오차율_구간": bootstrap_ci(abs_err, statistics.median),
        "과대": sum(1 for s in scored if s["오차율"] > 0),
        "과소": sum(1 for s in scored if s["오차율"] < 0),
        "적중률": statistics.mean(hits),
        "적중률_구간": bootstrap_ci(hits, statistics.mean),
        "폭_중앙값": statistics.median(widths) if widths else None,
    })
    return summary


def summarize(rows: list[dict]) -> dict:
    """건별 채점 결과를 묶음별(전체, 시공범위별, 플래그 없는 건)·공종별로 요약한다."""
    ok = [r for r in rows if "실패" not in r]
    groups = {
        "전체": ok,
        "전체 시공": [r for r in ok if r["시공범위"] == "전체"],
        "부분 시공": [r for r in ok if r["시공범위"] == "부분"],
        "플래그 없는 건": [r for r in ok if not r["flags"]],
    }
    return {
        "실패": [r["id"] for r in rows if "실패" in r],
        "총액": {name: summarize_scores([r["총액"] for r in group]) for name, group in groups.items()},
        "직접비": {name: summarize_scores([r["직접비"] for r in group]) for name, group in groups.items()},
        "공종별": {
            g: summarize_scores([r["공종별"][g] for r in ok if g in r["공종별"]])
            for g in 공종_순서 if any(g in r["공종별"] for r in ok)
        },
    }


# ── 출력 ──────────────────────────────────────────────────────────────────


def _pct(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "-"
    return f"{value:+.0%}" if signed else f"{value:.0%}"


def _ci(interval: tuple[float, float] | None) -> str:
    return f"[{interval[0]:.0%}~{interval[1]:.0%}]" if interval else "-"


def _summary_line(name: str, s: dict) -> str:
    if not s["건수"]:
        return f"  {name:<10} 채점 0건, 미산출 {s['미산출']}건"
    line = (f"  {name:<10} n={s['건수']:<3} 절대오차율 {_pct(s['절대오차율_중앙값'])} {_ci(s['절대오차율_구간'])}  "
            f"과대 {s['과대']}·과소 {s['과소']}  적중률 {_pct(s['적중률'])} {_ci(s['적중률_구간'])}  "
            f"폭 {_pct(s['폭_중앙값'])}")
    return line + (f"  미산출 {s['미산출']}" if s["미산출"] else "")


def print_report(rows: list[dict], summary: dict) -> None:
    print(f"\n{'id':<8}{'범위':<5}{'사례':>4}  {'총액(간접비 포함)':<22}{'직접비 기준':<22}flags")
    for r in rows:
        if "실패" in r:
            print(f"{r['id']:<8}{r['시공범위']:<5}  실패: {r['실패']}")
            continue
        cells = []
        for key in ("총액", "직접비"):
            s = r[key]
            cells.append(f"{_pct(s['오차율'], signed=True):>5} {'적중' if s['적중'] else '벗어남':<4} 폭 {_pct(s['폭']):<6}")
        print(f"{r['id']:<8}{r['시공범위']:<5}{r['참고_사례_수']:>4}  {cells[0]:<22}{cells[1]:<22}{','.join(r['flags'])}")

    for key, title in (("총액", "총액 — 간접비 포함 기준"), ("직접비", "총액 — 직접비 기준")):
        print(f"\n[{title}]")
        for name, s in summary[key].items():
            print(_summary_line(name, s))
    print("\n[공종별]")
    for g, s in summary["공종별"].items():
        print(_summary_line(g, s))

    전체 = summary["총액"]["전체"]
    if 전체["건수"]:
        print(f"\n범위 적중률 목표 {TARGET_HIT_RATE:.0%} / 실제 {_pct(전체['적중률'])} (간접비 포함), "
              f"{_pct(summary['직접비']['전체']['적중률'])} (직접비)")
    if summary["실패"]:
        print(f"견적을 못 낸 건: {summary['실패']}")


# ── 실행 ──────────────────────────────────────────────────────────────────


async def run(split: str) -> None:
    records = [r for r in json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
               if r["status"] == "verified" and r["split"] == split]
    if not records:
        raise SystemExit(f"검수 완료된 {split} 레코드가 없습니다")

    # app/core/deps.py의 lifespan과 같은 구성 — 서비스와 다른 설정으로 채점하면 의미가 없다
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]
    repo = LeaveOutCaseRepository(db["estimate_cases"], settings)
    embedder = SentenceTransformer(settings.embed_model)
    reranker = CrossEncoder(settings.reranker_model, max_length=512) if settings.use_reranker else None
    coefficients = await CoefficientRepository(collection=db["correction_coefficients"]).get_active()
    engine = EstimateEngine(
        case_repository=repo, embedder=embedder, reranker=reranker,
        vector_candidate_pool=settings.vector_candidate_pool, coefficients=coefficients,
    )

    rows, details = [], []
    for record in records:
        source = record["source"]
        left_out = await repo.leave_out(source.get("request_url"), source.get("article_id"))
        output = await engine.generate(dict(record["input"]))
        leaked = str(source.get("article_id")) in output.get("reference_case_ids", [])
        if leaked:
            raise SystemExit(f"{record['id']}: 정답 견적이 참고 사례에 들어갔습니다 — 제외가 동작하지 않음")
        row = score_record(record, output)
        rows.append(row)
        details.append({**row, "제외된_사례_수": left_out, "output": {
            k: output.get(k) for k in ("총_견적_범위", "공종별_단가_범위", "데이터_부족_공종", "reference_case_ids")
        }})
    client.close()

    summary = summarize(rows)
    print_report(rows, summary)

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{split}_{stamp}.json"
    path.write_text(json.dumps({
        "split": split, "run_at": stamp, "engine_version": output.get("engine_version"),
        "coefficient_version": output.get("coefficient_version"), "use_reranker": settings.use_reranker,
        "vector_candidate_pool": settings.vector_candidate_pool,
        "summary": summary, "records": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] {len(rows)}건 채점 → {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "eval"], default="dev")
    parser.add_argument("--final", action="store_true", help="평가용(eval) 세트 실행 확인 — 마지막 비교 때 한 번만")
    args = parser.parse_args()
    # 평가용 세트 결과를 보면서 엔진을 고치면 세트를 나눈 의미가 없어진다. 실수로 돌리지 않게 막는다
    if args.split == "eval" and not args.final:
        parser.error("평가용 세트는 마지막 비교 때만 실행합니다. 정말 실행하려면 --final")
    asyncio.run(run(args.split))


if __name__ == "__main__":
    main()
