"""평수 필터 허용치(SIZE_RANGE)를 ±5 / ±6 / ±7로 바꿔가며 production 검색 경로
(EstimateEngine.retrieve_cases() — Stage 1~5 필터 → BM25+RRF → cross-encoder, 비주거 제외)를
같은 라벨셋으로 비교한다.

서비스 코드는 건드리지 않는다. estimate_engine._build_filter()가 모듈 전역 SIZE_RANGE를
호출 시점에 읽기 때문에, 이 스크립트 안에서만 모듈 속성을 바꿔 끼운다.

두 단계로 나뉜다 — 라벨링 단계에서 검색 점수·순위를 보지 않도록(순환 논리 방지) 분리했다.

1) collect: 각 허용치로 retrieve_cases()를 돌려 top-15와 Stage/필터 통과 후보 수를
   eval/results/size_range_runs.json에 저장하고, labels.csv에 없는 (쿼리, 사례) 쌍을
   규칙(build_pool._suggested_relevant)으로 1차 판단한다.
     - 규칙이 명확한 행  -> eval/test_inputs/size6_rule_labels.csv  (label 채워짐)
     - 판단이 갈리는 행  -> eval/test_inputs/review_size6.csv      (label 비움, 사람 검토)
   두 CSV 모두 (query_id, article_id) 순으로 정렬하고 순위·점수 열은 넣지 않는다.
       python -m eval.size_range_comparison collect --size-ranges 5 6 7

2) evaluate: 저장된 runs + labels.csv로 P@5/10/15, 쿼리별 차이, Stage 분포,
   부트스트랩 구간을 계산한다 (DB 불필요).
       python -m eval.size_range_comparison evaluate

라벨 반영:
    python -m eval.apply_production_labels --review-file eval/test_inputs/size6_rule_labels.csv
    (review_size6.csv는 label 열을 채운 뒤 같은 명령으로 반영)
"""

import argparse
import asyncio
import csv
import json
import pathlib
import random

from dotenv import load_dotenv

ROOT = pathlib.Path(__file__).parent
QUERIES_PATH = ROOT / "test_inputs" / "queries.json"
LABELS_CSV_PATH = ROOT / "test_inputs" / "labels.csv"
RULE_LABELS_PATH = ROOT / "test_inputs" / "size6_rule_labels.csv"
REVIEW_PATH = ROOT / "test_inputs" / "review_size6.csv"
RUNS_PATH = ROOT / "results" / "size_range_runs.json"
EVAL_OUT_PATH = ROOT / "results" / "size_range_eval.json"

KS = (5, 10, 15)
BOOTSTRAP_N = 10_000


# ── collect ────────────────────────────────────────────

def _review_reason(query_input: dict, case: dict) -> tuple[bool, str]:
    """(판단이 갈리는지, 규칙 판단 근거). flag_review_rows.py 기준 중 순위를 쓰지 않는 2·3번만 적용."""
    from eval.build_pool import _ADJACENT_BUCKETS, _REGION_TO_BUCKET, _case_works, _suggested_relevant

    q_size = int(query_input.get("평수") or 0)
    c_size = int(case.get("size_pyeong") or 0)
    diff = abs(q_size - c_size)
    q_bucket = query_input.get("지역", "서울")
    c_bucket = _REGION_TO_BUCKET.get(case.get("region"), "지방")
    region_ok = c_bucket in _ADJACENT_BUCKETS.get(q_bucket, {q_bucket})
    q_works = set(query_input.get("공종", []))
    overlap = q_works & _case_works(case)
    works_ok = not q_works or bool(overlap)
    suggested = _suggested_relevant(query_input, case)

    basis = (f"평수차 {diff}({'≤5' if diff <= 5 else '>5'}), "
             f"지역 {q_bucket}↔{case.get('region')}({c_bucket}, {'인접' if region_ok else '비인접'}), "
             f"공종 겹침 {len(overlap)}개[{','.join(sorted(overlap))}]")

    if not suggested and 6 <= diff <= 7 and region_ok and works_ok:
        return True, "borderline_size: 평수만 규칙(±5) 밖, 지역·공종은 통과 — " + basis
    if suggested and len(overlap) == 1 and len(q_works) > 1:
        return True, "weak_overlap_one: 요청 공종 중 1개만 겹침 — " + basis
    return False, basis


async def collect(size_ranges: list[int]) -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    from sentence_transformers import CrossEncoder, SentenceTransformer

    import app.domain.estimate_engine as ee
    from app.core.config import get_settings
    from app.repositories.case_repository import CaseRepository
    from eval.build_pool import _case_works, _suggested_relevant

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]
    repo = CaseRepository(col, settings)
    embedder = SentenceTransformer(settings.embed_model)
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    engine = ee.EstimateEngine(case_repository=repo, embedder=embedder, reranker=reranker)

    # retrieve_cases()가 남기는 log_event("retrieve_cases", stage=..., pool_size=...)를 가로채
    # 어느 Stage에서 멈췄는지 기록한다 (서비스 코드 무수정).
    captured: list[dict] = []
    original_log_event = ee.log_event

    def _capturing_log_event(event, **kw):
        if event == "retrieve_cases":
            captured.append(kw)
        return original_log_event(event, **kw)

    ee.log_event = _capturing_log_event
    original_size_range = ee.SIZE_RANGE

    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))
    labeled_keys = set()
    with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            labeled_keys.add((row["query_id"], row["article_id"]))

    runs: dict[str, dict] = {}
    unlabeled: dict[tuple[str, str], tuple[dict, dict]] = {}
    try:
        for sr in size_ranges:
            ee.SIZE_RANGE = sr
            per_query = {}
            for q in queries:
                qid, inp = q["query_id"], q["input"]
                captured.clear()
                cases = await engine.retrieve_cases(engine.build_query(inp), inp)
                ev = captured[-1] if captured else {}
                stage = ev.get("stage")

                # 필터 통과 후보 수: 벡터 후보 풀(40건 캡)과 무관하게 조건을 만족하는 전체 사례 수.
                # 채택 Stage의 조건 + 비주거 제외($match와 동일)로 센다.
                flags = [(True, True, True, True), (True, True, True, False), (True, False, True, False),
                         (False, False, True, False), (False, False, False, False)][(stage or 5) - 1]
                지역들 = ee.REGION_MAP.get(inp.get("지역", "서울"), ["서울"])
                grade = ee.자재등급_TO_GRADE.get(inp.get("자재등급", "중급"), "중급")
                mf = engine._build_filter(int(inp.get("평수") or 0), 지역들, inp.get("공종", []),
                                          use_size=flags[0], use_region=flags[1], use_has=flags[2],
                                          use_grade=flags[3], grade=grade)
                # 참고용: Stage 2 조건(평수+지역+공종) 통과 수 — 허용치별 사례 확보량 비교용
                mf2 = engine._build_filter(int(inp.get("평수") or 0), 지역들, inp.get("공종", []),
                                           use_size=True, use_region=True, use_has=True, use_grade=False)
                resid = {"is_non_residential": {"$ne": True}}
                n_adopted = await col.count_documents({"$and": [mf, resid]} if mf else resid)
                n_stage2 = await col.count_documents({"$and": [mf2, resid]})

                top15 = [str(c.get("article_id")) for c in cases[:15]]
                per_query[qid] = {
                    "stage": stage,
                    "fallback": ev.get("fallback"),
                    "vector_pool": ev.get("pool_size"),
                    "filter_pass_adopted_stage": n_adopted,
                    "filter_pass_stage2": n_stage2,
                    "top15": top15,
                }
                for c in cases[:15]:
                    key = (qid, str(c.get("article_id")))
                    if key not in labeled_keys:
                        unlabeled.setdefault(key, (inp, c))
            runs[str(sr)] = per_query
            print(f"[SIZE_RANGE=±{sr}] 완료")
    finally:
        ee.SIZE_RANGE = original_size_range
        ee.log_event = original_log_event
        client.close()

    RUNS_PATH.write_text(json.dumps(runs, ensure_ascii=False, indent=2), encoding="utf-8")

    rule_rows, review_rows = [], []
    for (qid, aid) in sorted(unlabeled):
        inp, c = unlabeled[(qid, aid)]
        needs_review, basis = _review_reason(inp, c)
        suggested = int(_suggested_relevant(inp, c))
        base = {
            "query_id": qid,
            "query_size": inp.get("평수"),
            "query_region": inp.get("지역"),
            "query_works": ",".join(inp.get("공종", [])),
            "article_id": aid,
            "region": c.get("region"),
            "size_pyeong": c.get("size_pyeong"),
            "works": ",".join(sorted(_case_works(c))),
            "suggested_relevant": suggested,
        }
        if needs_review:
            review_rows.append({
                **base,
                "size_diff": abs(int(inp.get("평수") or 0) - int(c.get("size_pyeong") or 0)),
                "rule_basis": basis,
                "post_title": (c.get("post_title") or "").strip(),
                "post_url": c.get("post_url") or "",
                "label": "",
            })
        else:
            rule_rows.append({**base, "label": suggested})

    base_fields = ["query_id", "query_size", "query_region", "query_works",
                   "article_id", "region", "size_pyeong", "works", "suggested_relevant"]
    with RULE_LABELS_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=base_fields + ["label"])
        w.writeheader()
        w.writerows(rule_rows)
    with REVIEW_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["query_id", "article_id", "size_diff", "rule_basis"]
                           + [k for k in base_fields if k not in ("query_id", "article_id")]
                           + ["post_title", "post_url", "label"])
        w.writeheader()
        w.writerows(review_rows)

    print(f"[OK] runs -> {RUNS_PATH}")
    print(f"     라벨 없는 후보 {len(unlabeled)}건: 규칙 확정 {len(rule_rows)}건 -> {RULE_LABELS_PATH.name}, "
          f"검토 대기 {len(review_rows)}건 -> {REVIEW_PATH.name}")


# ── evaluate ───────────────────────────────────────────

def _load_labels() -> dict[tuple[str, str], int]:
    labels = {}
    with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            labels[(row["query_id"], row["article_id"])] = int(row["label"])
    return labels


def _prec(qid: str, ids: list[str], k: int, labels: dict) -> tuple[int, int, int]:
    """(relevant, labeled, unlabeled) within top-k. 후보가 k개 미만이면 있는 만큼만 센다."""
    rel = lab = unl = 0
    for aid in ids[:k]:
        if (qid, aid) in labels:
            lab += 1
            rel += labels[(qid, aid)]
        else:
            unl += 1
    return rel, lab, unl


def _micro(per_q: list[tuple[int, int]]) -> float:
    rel = sum(r for r, _ in per_q)
    lab = sum(n for _, n in per_q)
    return rel / lab if lab else float("nan")


def evaluate() -> None:
    runs = json.loads(RUNS_PATH.read_text(encoding="utf-8"))
    labels = _load_labels()
    qids = sorted(next(iter(runs.values())).keys())
    out: dict = {"size_ranges": {}, "paired": {}}

    for sr, per_query in runs.items():
        res = {"k": {}, "per_query": {}, "stage_dist": {}, "coverage": None}
        for k in KS:
            pq = [_prec(q, per_query[q]["top15"], k, labels) for q in qids]
            macro_vals = [r / n for r, n, _ in pq if n]
            res["k"][k] = {
                "micro": _micro([(r, n) for r, n, _ in pq]),
                "macro": sum(macro_vals) / len(macro_vals) if macro_vals else float("nan"),
                "labeled": sum(n for _, n, _ in pq),
                "unlabeled": sum(u for _, _, u in pq),
            }
        for q in qids:
            r, n, u = _prec(q, per_query[q]["top15"], 15, labels)
            info = per_query[q]
            res["per_query"][q] = {"rel": r, "lab": n, "unl": u, "p15": r / n if n else None,
                                   "n_top": len(info["top15"]), "stage": info["stage"],
                                   "filter_pass_adopted_stage": info["filter_pass_adopted_stage"],
                                   "filter_pass_stage2": info["filter_pass_stage2"]}
            res["stage_dist"][str(info["stage"])] = res["stage_dist"].get(str(info["stage"]), 0) + 1
        k15 = res["k"][15]
        res["coverage"] = k15["labeled"] / (k15["labeled"] + k15["unlabeled"])
        out["size_ranges"][sr] = res

    # 대응표본 비교: 쿼리 단위 부트스트랩 (micro P@15 차이), 쿼리별 증감 개수
    rng = random.Random(20260929)
    pairs = [(a, b) for a in runs for b in runs if int(a) < int(b)]
    for a, b in pairs:
        pa = out["size_ranges"][a]["per_query"]
        pb = out["size_ranges"][b]["per_query"]
        obs = _micro([(pb[q]["rel"], pb[q]["lab"]) for q in qids]) - _micro([(pa[q]["rel"], pa[q]["lab"]) for q in qids])
        diffs = []
        for _ in range(BOOTSTRAP_N):
            s = [rng.choice(qids) for _ in qids]
            da = _micro([(pa[q]["rel"], pa[q]["lab"]) for q in s])
            db = _micro([(pb[q]["rel"], pb[q]["lab"]) for q in s])
            diffs.append(db - da)
        diffs.sort()
        up = sum(1 for q in qids if pa[q]["p15"] is not None and pb[q]["p15"] is not None and pb[q]["p15"] > pa[q]["p15"])
        down = sum(1 for q in qids if pa[q]["p15"] is not None and pb[q]["p15"] is not None and pb[q]["p15"] < pa[q]["p15"])
        out["paired"][f"{b}-{a}"] = {
            "obs_diff_micro_p15": obs,
            "ci95": [diffs[int(0.025 * BOOTSTRAP_N)], diffs[int(0.975 * BOOTSTRAP_N) - 1]],
            "queries_up": up, "queries_down": down, "queries_same": len(qids) - up - down,
            "identical_top15": sum(1 for q in qids if runs[a][q]["top15"] == runs[b][q]["top15"]),
        }

    EVAL_OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 콘솔 요약
    for sr, res in out["size_ranges"].items():
        tag = "" if res["coverage"] == 1 else "  (잠정 — 커버리지 100% 아님)"
        ks = "  ".join(f"P@{k} {res['k'][k]['micro']:.1%}/{res['k'][k]['macro']:.1%}" for k in KS)
        print(f"±{sr}: {ks}  (micro/macro)  커버리지 {res['coverage']:.1%}  Stage {res['stage_dist']}{tag}")
    print()
    print(f"{'q':<5}" + "".join(f"{'±' + sr + ' P@15':>12}{'통과':>6}" for sr in runs))
    for q in qids:
        line = f"{q:<5}"
        for sr in runs:
            pq = out["size_ranges"][sr]["per_query"][q]
            p = f"{pq['p15']:.0%}" if pq["p15"] is not None else "N/A"
            if pq["unl"]:
                p += f"*{pq['unl']}"
            line += f"{p:>12}{pq['filter_pass_adopted_stage']:>6}"
        print(line)
    print("(* = top-15 중 라벨 없는 건수)")
    print()
    for key, v in out["paired"].items():
        print(f"±{key.replace('-', ' vs ±')}: 차이 {v['obs_diff_micro_p15']:+.1%}p, "
              f"95% CI [{v['ci95'][0]:+.1%}, {v['ci95'][1]:+.1%}], "
              f"상승 {v['queries_up']} / 하락 {v['queries_down']} / 동일 {v['queries_same']}, "
              f"top-15 완전 동일 {v['identical_top15']}개 쿼리")
    print(f"[OK] -> {EVAL_OUT_PATH}")


def main() -> None:
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--size-ranges", type=int, nargs="+", default=[5, 6, 7])
    sub.add_parser("evaluate")
    args = parser.parse_args()
    if args.cmd == "collect":
        asyncio.run(collect(args.size_ranges))
    else:
        evaluate()


if __name__ == "__main__":
    main()
