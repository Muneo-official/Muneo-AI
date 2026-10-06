"""
eval/quote_benchmark.py — 가견적 채점: 정답셋의 입력으로 엔진을 돌려 실제 견적서 금액과 비교한다.

    python -m eval.quote_benchmark                  # 개선용(dev) 세트 채점
    python -m eval.quote_benchmark --split eval --final   # 평가용 세트 — 마지막 비교 때 한 번만
    python -m eval.quote_benchmark --split holdout --final   # 검증용 세트 — 평가용을 연 뒤의 수정을 검증할 때

`/estimates/generate`가 부르는 EstimateEngine.generate()를 같은 설정(리랭커 사용 여부, 후보 풀, 활성 보정계수)으로
직접 호출한다. 정답으로 뽑은 견적은 코퍼스에 그대로 남아 있으므로, 채점할 때는 같은 의뢰(request_url)의 사례를
검색 결과에서 뺀다 — 안 빼면 엔진이 자기 답을 참고 사례로 쓴다. 같은 의뢰에 여러 업체 견적이 달리므로 게시글이
아니라 의뢰 단위로 뺀다.

보는 값
  - 총액 오차율: (엔진 중간값 − 정답) / 정답. 절대값의 중앙값과 과대·과소 건수
  - 범위 적중률: 정답이 [최소, 최대] 안에 든 비율. 범위를 넓게 부르면 올라가므로 폭((최대−최소)/중간)과 함께 본다
  - 같은 폭 적중률: 부른 범위 대신 중간값 ±13.5%(폭 27%) 안에 정답이 든 비율. 범위를 넓게 부르는 쪽과
    좁게 부르는 쪽을 같은 잣대로 비교한다(eval/quote_llm_benchmark.py의 LLM 비교)
  - 주 지표는 공사비(직접비) 기준이다 — 가견적은 견적서의 공사비만 대상으로 하고 이윤·보험료·부가세는 뺀다.
    간접비 포함 기준은 참고로 함께 낸다(코퍼스의 total_cost에 간접비 포함 여부가 섞여 있다)
  - 전체 시공과 부분 시공은 총액 산출 경로가 달라 나눠서 보고, 플래그가 붙은 건을 뺀 값도 함께 낸다
  - 구간은 정답 레코드 단위 부트스트랩 95%
  - 엔진이 견적을 못 낸 건은 적중률에서 '벗어남'으로 센다 — 빼고 세면 어려운 건을 실패시키는 변경이 개선처럼 보인다

건별 결과는 커밋하지 않는 위치(estimate_data/_gt_review/benchmark_runs/)에 저장한다.
"""

import argparse
import asyncio
import datetime
import json
import random
import statistics
import sys

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import ENGINE_VERSION, EstimateEngine
from app.repositories.case_repository import CaseRepository
from app.repositories.coefficient_repository import CoefficientRepository
from app.schemas.estimate import EstimateRequest
from eval.quote_ground_truth import GROUND_TRUTH_PATH, HOLDOUT, REVIEW_DIR, 공종_순서

load_dotenv()

SPLITS = ["dev", "eval", HOLDOUT]
TARGET_HIT_RATE = 0.80
# 같은 폭 적중률의 폭 — 엔진이 참고 사례를 12건 이상 모았을 때 부르는 총액 범위의 폭. 중간값에서 위아래로 절반씩
COMMON_WIDTH = 0.27
RUNS_DIR = REVIEW_DIR / "benchmark_runs"
BOOTSTRAP_N = 2000
BOOTSTRAP_SEED = 68


class LeaveOutCaseRepository(CaseRepository):
    """채점 중인 정답 견적과 같은 의뢰의 사례를 검색 결과에서 빼는 CaseRepository.

    서비스 코드는 건드리지 않고 엔진에 주입하는 저장소만 바꾼다. 그 사례들이 코퍼스에 없을 때 서비스가
    돌려줄 결과와 같아야 하므로, CaseRepository.vector_search()의 파이프라인을 순서만 바꿔 다시 쓴다 —
    빠질 건수만큼 더 가져와 먼저 빼고, limit으로 자른 다음에 비주거 사례를 거른다. 비주거를 먼저 거르면
    그 빈자리가 서비스에는 없는 후순위 사례로 채워진다.
    """

    def __init__(self, collection, settings):
        super().__init__(collection, settings)
        self.left_out_ids: set[str] = set()

    async def leave_out(self, request_url: str | None, article_id: str | None) -> int:
        """이후 검색에서 뺄 의뢰를 정한다. 반환: 코퍼스에서 빠지는 사례 수."""
        conds = []
        if request_url:
            conds.append({"request_url": request_url})
        if article_id:
            conds.append({"article_id": str(article_id)})
        self.left_out_ids = set()
        if conds:
            cursor = self._collection.find({"$or": conds}, {"article_id": 1, "_id": 0})
            self.left_out_ids = {str(doc["article_id"]) async for doc in cursor}
        return len(self.left_out_ids)

    async def vector_search(self, query_embedding, mongo_filter, limit, num_candidates=150):
        fetch = limit + len(self.left_out_ids)
        stage: dict = {
            "index": self._index_name,
            "path": "embedding",
            "queryVector": query_embedding,
            # $vectorSearch의 limit은 numCandidates를 넘을 수 없다. 더 가져오는 만큼 같이 늘린다
            "numCandidates": max(num_candidates, fetch),
            "limit": fetch,
        }
        if mongo_filter:
            stage["filter"] = mongo_filter
        cursor = self._collection.aggregate([{"$vectorSearch": stage}, {"$project": {"embedding": 0}}])
        ranked = [doc async for doc in cursor if str(doc.get("article_id")) not in self.left_out_ids]
        return [doc for doc in ranked[:limit] if doc.get("is_non_residential") is not True]


# ── 채점 (순수 계산) ──────────────────────────────────────────────────────


def score_range(rng: dict | None, truth: int) -> dict | None:
    """범위 하나를 정답 금액 하나와 비교한다. 범위가 없거나 정답이 0 이하면 None."""
    if not rng or truth <= 0:
        return None
    lo, mid, hi = rng["최소"], rng["중간"], rng["최대"]
    return {
        "오차율": (mid - truth) / truth,
        "적중": lo <= truth <= hi,
        "같은폭_적중": mid * (1 - COMMON_WIDTH / 2) <= truth <= mid * (1 + COMMON_WIDTH / 2),
        "폭": (hi - lo) / mid if mid > 0 else None,
    }


def score_record(record: dict, output: dict) -> dict:
    """정답 레코드 하나와 엔진 출력 하나를 채점한다. 엔진이 견적을 못 냈으면 실패로 남긴다."""
    truth = record["truth"]
    row = {
        "id": record["id"],
        "시공범위": record["input"]["시공범위"],
        "flags": list(record["flags"]),
    }
    if "error" in output:
        # 공종별 적중률에서도 '벗어남'으로 세려면 어떤 공종이 정답에 있었는지 남겨야 한다
        return {**row, "실패": output["error"], "공종": list(truth["공종별"])}

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


def summarize_scores(scores: list[dict | None], failed: int = 0) -> dict:
    """score_range() 결과 여러 개를 한 줄 요약으로.

    None(미산출)은 건수만 세고 지표에서 뺀다. failed는 엔진이 견적 자체를 못 낸 건수로, 오차율과 폭은
    구할 수 없으니 빼고 적중률에서만 '벗어남'으로 센다.
    """
    scored = [s for s in scores if s is not None]
    summary = {"건수": len(scored), "미산출": len(scores) - len(scored), "실패": failed}
    if not scored:
        if failed:  # 전부 실패한 묶음은 적중률 0%다. 값을 비워 두면 회차 평균에서 이 회차가 빠진다
            summary.update({"적중률": 0.0, "같은폭_적중률": 0.0})
        return summary
    abs_err = [abs(s["오차율"]) for s in scored]
    hits = [1.0 if s["적중"] else 0.0 for s in scored] + [0.0] * failed
    common_hits = [1.0 if s["같은폭_적중"] else 0.0 for s in scored] + [0.0] * failed
    widths = [s["폭"] for s in scored if s["폭"] is not None]
    summary.update({
        "절대오차율_중앙값": statistics.median(abs_err),
        "절대오차율_구간": bootstrap_ci(abs_err, statistics.median),
        "과대": sum(1 for s in scored if s["오차율"] > 0),
        "과소": sum(1 for s in scored if s["오차율"] < 0),
        "적중률": statistics.mean(hits),
        "적중률_구간": bootstrap_ci(hits, statistics.mean),
        "같은폭_적중률": statistics.mean(common_hits),
        "같은폭_적중률_구간": bootstrap_ci(common_hits, statistics.mean),
        "폭_중앙값": statistics.median(widths) if widths else None,
    })
    return summary


def summarize(rows: list[dict]) -> dict:
    """건별 채점 결과를 묶음별(전체, 시공범위별, 플래그 없는 건)·공종별로 요약한다."""
    groups = {
        "전체": rows,
        "전체 시공": [r for r in rows if r["시공범위"] == "전체"],
        "부분 시공": [r for r in rows if r["시공범위"] == "부분"],
        "플래그 없는 건": [r for r in rows if not r["flags"]],
    }
    ok = [r for r in rows if "실패" not in r]
    failed = [r for r in rows if "실패" in r]

    def by_group(key: str) -> dict:
        return {
            name: summarize_scores([r[key] for r in group if "실패" not in r],
                                   failed=sum(1 for r in group if "실패" in r))
            for name, group in groups.items()
        }

    def has_공종(row: dict, g: str) -> bool:
        return g in (row["공종"] if "실패" in row else row["공종별"])

    return {
        "실패": [r["id"] for r in failed],
        "총액": by_group("총액"),
        "직접비": by_group("직접비"),
        "공종별": {
            g: summarize_scores([r["공종별"][g] for r in ok if g in r["공종별"]],
                                failed=sum(1 for r in failed if g in r["공종"]))
            for g in 공종_순서 if any(has_공종(r, g) for r in rows)
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
        return f"  {name:<10} 채점 0건, 미산출 {s['미산출']}건, 실패 {s['실패']}건"
    line = (f"  {name:<10} n={s['건수']:<3} 절대오차율 {_pct(s['절대오차율_중앙값'])} {_ci(s['절대오차율_구간'])}  "
            f"과대 {s['과대']}·과소 {s['과소']}  적중률 {_pct(s['적중률'])} {_ci(s['적중률_구간'])}  "
            f"폭 {_pct(s['폭_중앙값'])}  같은 폭(±{COMMON_WIDTH / 2:.1%}) 적중률 {_pct(s['같은폭_적중률'])}")
    line += f"  미산출 {s['미산출']}" if s["미산출"] else ""
    return line + (f"  실패 {s['실패']}(벗어남으로 셈)" if s["실패"] else "")


def print_report(rows: list[dict], summary: dict) -> None:
    print(f"\n{'id':<8}{'범위':<5}{'사례':>4}  {'공사비 기준':<22}{'간접비 포함(참고)':<22}flags")
    for r in rows:
        if "실패" in r:
            print(f"{r['id']:<8}{r['시공범위']:<5}  실패: {r['실패']}")
            continue
        cells = []
        for key in ("직접비", "총액"):
            s = r[key]
            if s is None:  # 비교할 정답 금액이 0 이하인 건
                cells.append("채점 불가")
                continue
            cells.append(f"{_pct(s['오차율'], signed=True):>5} {'적중' if s['적중'] else '벗어남':<4} 폭 {_pct(s['폭']):<6}")
        print(f"{r['id']:<8}{r['시공범위']:<5}{r['참고_사례_수']:>4}  {cells[0]:<22}{cells[1]:<22}{','.join(r['flags'])}")

    for key, title in (("직접비", "총액 — 공사비 기준 (주 지표)"), ("총액", "총액 — 간접비 포함 기준 (참고)")):
        print(f"\n[{title}]")
        for name, s in summary[key].items():
            print(_summary_line(name, s))
    print("\n[공종별]")
    for g, s in summary["공종별"].items():
        print(_summary_line(g, s))

    print(f"\n범위 적중률 목표 {TARGET_HIT_RATE:.0%} / 실제 {_pct(summary['직접비']['전체'].get('적중률'))} (공사비 기준), "
          f"{_pct(summary['총액']['전체'].get('적중률'))} (간접비 포함)")
    if summary["실패"]:
        print(f"견적을 못 낸 건: {summary['실패']}")


# ── 실행 ──────────────────────────────────────────────────────────────────


def load_records(split: str) -> list[dict]:
    """채점할 정답 레코드. LLM 비교(eval/quote_llm_benchmark.py)도 이 함수를 쓴다 — 두 쪽이 다른 건을 채점하지 않게."""
    records = [r for r in json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
               if r["status"] == "verified" and r["split"] == split]
    if not records:
        raise SystemExit(f"검수 완료된 {split} 레코드가 없습니다")
    return records


def require_final(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """평가용·검증용 세트 결과를 보면서 엔진이나 프롬프트를 고치면 세트를 나눈 의미가 없어진다. 실수로 돌리지 않게 막는다."""
    if args.split != "dev" and not args.final:
        parser.error("평가용·검증용 세트는 마지막 비교 때만 실행합니다. 정말 실행하려면 --final")


async def build_engine():
    """채점에 쓰는 엔진. 반환: (Mongo 클라이언트, 설정, 의뢰를 빼는 저장소, 엔진, 활성 보정계수).

    app/core/deps.py의 lifespan과 같은 구성 — 서비스와 다른 설정으로 채점하면 의미가 없다. lifespan에 엔진 인자나
    설정이 추가되면 여기를 고친다. 리스크 채점(eval/risk_benchmark.py)도 이 함수를 쓴다.
    """
    settings = get_settings()
    client = AsyncIOMotorClient(
        settings.mongo_uri,
        maxPoolSize=settings.mongo_max_pool_size,
        serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms,
    )
    db = client[settings.mongo_db_name]
    repo = LeaveOutCaseRepository(db["estimate_cases"], settings)
    embedder = SentenceTransformer(settings.embed_model)
    reranker = CrossEncoder(settings.reranker_model, max_length=512) if settings.use_reranker else None
    coefficients = await CoefficientRepository(collection=db["correction_coefficients"]).get_active()
    engine = EstimateEngine(
        case_repository=repo, embedder=embedder, reranker=reranker,
        vector_candidate_pool=settings.vector_candidate_pool, coefficients=coefficients,
        window_includes_door=settings.estimate_window_includes_door,
    )
    return client, settings, repo, engine, coefficients


async def run(split: str) -> None:
    records = load_records(split)
    client, settings, repo, engine, coefficients = await build_engine()

    rows, details = [], []
    for record in records:
        source = record["source"]
        left_out = await repo.leave_out(source.get("request_url"), source.get("article_id"))
        # 라우터와 같은 경로로 입력을 만든다 — 스키마의 기본값·정규화를 거친 값이 엔진에 들어간다
        inp = EstimateRequest(**record["input"]).model_dump(exclude_none=True)
        output = await engine.generate(inp)
        leaked = sorted(repo.left_out_ids & set(output.get("reference_case_ids", [])))
        if leaked:
            raise SystemExit(f"{record['id']}: 제외한 사례가 참고 사례에 들어갔습니다 {leaked} — 제외가 동작하지 않음")
        row = score_record(record, output)
        rows.append(row)
        details.append({**row, "제외된_사례_수": left_out, "output": {
            k: output.get(k) for k in ("총_견적_범위", "공종별_단가_범위", "데이터_부족_공종", "reference_case_ids")
        }})
    client.close()

    summary = summarize(rows)
    # 출력 중에 문제가 생겨도 결과가 남도록 먼저 저장한다
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{split}_{stamp}.json"
    path.write_text(json.dumps({
        "split": split, "run_at": stamp, "engine_version": ENGINE_VERSION,
        "coefficient_version": coefficients.get("version", "default"), "use_reranker": settings.use_reranker,
        "vector_candidate_pool": settings.vector_candidate_pool,
        "summary": summary, "records": details,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print_report(rows, summary)
    no_source = [r["id"] for r in records if not (r["source"].get("request_url") or r["source"].get("article_id"))]
    if no_source:
        print(f"출처 정보가 없어 제외를 걸지 못한 건: {no_source} — 코퍼스에 없는 견적인지 따로 확인해야 한다")
    print(f"\n[OK] {len(rows)}건 채점 → {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=SPLITS, default="dev")
    parser.add_argument("--final", action="store_true", help="평가용(eval)·검증용(holdout) 세트 실행 확인 — 마지막 비교 때 한 번만")
    args = parser.parse_args()
    require_final(parser, args)
    sys.stdout.reconfigure(encoding="utf-8")  # 출력을 파일로 돌리면 Windows 기본 인코딩(cp949)이라 '—'에서 멈춘다
    asyncio.run(run(args.split))


if __name__ == "__main__":
    main()
