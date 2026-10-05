"""
eval/risk_benchmark.py — 리스크 진단 채점: 결함을 심은 견적서에서 그 결함을 찾는지 본다.

    python -m eval.risk_benchmark                     # 개선용(dev) 세트 채점
    python -m eval.risk_benchmark --split eval --final   # 평가용 세트 — 마지막 비교 때 한 번만

견적서마다 깨끗한 판과 결함 판 두 이미지를 `/risk-detector/analyze`가 부르는 RiskDetectorService.analyze()에 넣는다.
두 판의 차이는 심은 결함뿐이므로, 결함 판에서 새로 생긴 지적만 따진다.

보는 값
  - 찾은 비율: 심은 결함 중, 결함 판에서 새로 생긴 지적이 가리킨 것의 비율
  - 새 지적의 적중률: 결함 판에서 새로 생긴 지적 중 심은 결함을 가리킨 것의 비율. 낮으면 근거 없는 지적이 많다
  - 깨끗한 판의 지적 수: 결함을 심지 않은 견적서에 지적한 수. 원본의 실제 문제도 섞여 있어 오탐 수는 아니다

Vision 파싱은 과금이다. 이미지의 파싱 결과를 파일에 저장해 두고 다시 부르지 않으므로, 판정 규칙만 고친 뒤에는
다시 돌려도 비용이 들지 않는다. 가격 비교의 참고 사례에서는 채점하는 견적서와 같은 의뢰의 사례를 뺀다.
"""

import argparse
import asyncio
import datetime
import json
import re
import sys
from collections import Counter

from dotenv import load_dotenv

from eval.risk_defects import CARRYING_WORDS, DEFECT_TYPES
from eval.risk_ground_truth import RISK_DIR, RISK_GT_PATH, image_paths

load_dotenv()

RUNS_DIR = RISK_DIR / "runs"
PARSE_CACHE_PATH = RISK_DIR / "parse_cache.json"
COMMON = "공통"  # 공종에 속하지 않는 지적(양중·운반 등)

# 견적서의 섹션 이름에 든 낱말 → 그 섹션을 가리키는 것으로 인정하는 공종 이름.
# 시스템마다 공종을 부르는 말이 달라서(욕실공사의 타일을 "타일"로도 "욕실"로도 부른다) 묶음으로 본다
SECTION_TRADES = [
    (("창호", "샷시", "도어"), {"창호", "도어", "샷시"}),
    (("도기", "수전", "욕실", "타일", "방수", "설비", "난방", "조적"), {"욕실", "타일", "설비", "도기", "수전", "방수"}),
    (("가구", "싱크", "주방"), {"가구", "주방"}),
    (("전기", "조명"), {"전기/조명", "전기", "조명"}),
    (("도장",), {"도장"}),
    (("시트", "필름"), {"필름", "시트"}),
    (("바닥",), {"바닥", "마루", "장판"}),
    (("도배",), {"도배"}),
    (("목공",), {"목공", "도어"}),
    (("철거",), {"철거"}),
    (("기타",), {"공과잡비", "기타", "마감", "마감/공과잡비"}),
    (("확장",), {"확장"}),
]


def trades_of(section: str) -> set[str]:
    """섹션 이름이 가리키는 공종 묶음. "목공,도어"처럼 두 공종이 묶인 섹션은 합집합이다."""
    out: set[str] = set()
    for words, trades in SECTION_TRADES:
        if any(w in section for w in words):
            out |= trades
    return out or {section}


# ── 지적 목록 ─────────────────────────────────────────────────────────────

_PRICE_RE = re.compile(r"견적 금액 ([\d,]+)원.*?\(([\d,]+)~([\d,]+)원")


def muneo_findings(report: dict) -> list[dict]:
    """리스크 진단 응답에서 지적 목록을 꺼낸다. 지적 하나는 (공종, 종류, 방향, 글)."""
    out = []
    for section in report["report"]["process_sections"]:
        for item in section["items"]:
            if item["status"] == "정상":
                continue
            text = f"{item['title']} {item['description']}"
            kind, trade, direction, extra = item["status"], section["process"], None, ""
            if "시세 범위" in item["title"]:
                # 화면에는 "불분명"으로 나가지만 가격 지적이다. 방향은 견적 금액이 범위의 어느 쪽에 있는지로 정한다
                kind = "가격"
                m = _PRICE_RE.search(item["description"])
                if m:
                    amount, lo, hi = (int(g.replace(",", "")) for g in m.groups())
                    direction = "과다" if amount > hi else "과소" if amount < lo else None
                    extra = str(amount)
            elif any(w in text for w in CARRYING_WORDS):
                kind, trade = "누락", COMMON
            out.append({"trade": trade, "kind": kind, "direction": direction, "extra": extra, "text": text})
    return out


def _key(finding: dict) -> tuple:
    """같은 지적인지 가리는 열쇠. 가격 지적은 금액이 달라지면 다른 지적이다."""
    return (finding["kind"], finding["trade"], finding["extra"])


def new_findings(clean: list[dict], planted: list[dict]) -> list[dict]:
    """결함 판의 지적 중 깨끗한 판에는 없던 것. 같은 지적이 여러 번이면 늘어난 만큼만 새 지적이다."""
    seen = Counter(_key(f) for f in clean)
    out = []
    for f in planted:
        if seen[_key(f)] > 0:
            seen[_key(f)] -= 1
        else:
            out.append(f)
    return out


def points_at(finding: dict, defect: dict) -> bool:
    """지적이 심은 결함을 가리키는가 — 같은 종류의 문제이고, 같은 공종이어야 한다."""
    if finding["kind"] != defect["kind"]:
        return False
    if defect["section"] is None:  # 조건 누락(M2): 공종 없이 양중·운반을 말하면 된다
        return finding["trade"] == COMMON or any(w in finding["text"] for w in CARRYING_WORDS)
    trades = trades_of(defect["section"]) | (trades_of(defect["also"]) if "also" in defect else set())
    if finding["trade"] not in trades:
        return False
    return "direction" not in defect or finding["direction"] in (None, defect["direction"])


def score_record(planted_defects: list[dict], clean: list[dict], planted: list[dict]) -> dict:
    fresh = new_findings(clean, planted)
    found = [any(points_at(f, d) for f in fresh) for d in planted_defects]
    return {
        "defects": [{"type": d["type"], "section": d["section"], "found": hit} for d, hit in zip(planted_defects, found)],
        "clean_count": len(clean), "planted_count": len(planted), "new_count": len(fresh),
        "new_hits": sum(any(points_at(f, d) for d in planted_defects) for f in fresh),
    }


def summarize(rows: list[dict]) -> dict:
    defects = [d for r in rows for d in r["defects"]]
    by_type = {t: [d["found"] for d in defects if d["type"] == t] for t in DEFECT_TYPES}
    new_total = sum(r["new_count"] for r in rows)
    return {
        "견적서": len(rows), "심은_결함": len(defects), "찾은_결함": sum(d["found"] for d in defects),
        "찾은_비율": sum(d["found"] for d in defects) / len(defects) if defects else None,
        "유형별": {t: {"심음": len(v), "찾음": sum(v)} for t, v in by_type.items() if v},
        "새_지적": new_total, "새_지적_적중": sum(r["new_hits"] for r in rows),
        "새_지적_적중률": sum(r["new_hits"] for r in rows) / new_total if new_total else None,
        "깨끗한_판_지적_수_평균": sum(r["clean_count"] for r in rows) / len(rows) if rows else None,
    }


def print_report(rows: list[dict], summary: dict) -> None:
    pct = lambda v: "-" if v is None else f"{v:.0%}"  # noqa: E731
    print("\nid      깨끗한 판  결함 판  새 지적(적중)  심은 결함")
    for r in rows:
        marks = "  ".join(f"{d['type']}{'○' if d['found'] else '×'}({d['section'] or COMMON})" for d in r["defects"])
        print(f"{r['id']}  {r['clean_count']:>6}  {r['planted_count']:>6}  {r['new_count']:>5}({r['new_hits']})      {marks}")
    print(f"\n찾은 비율: {summary['찾은_결함']}/{summary['심은_결함']} ({pct(summary['찾은_비율'])})")
    print(f"새 지적의 적중률: {summary['새_지적_적중']}/{summary['새_지적']} ({pct(summary['새_지적_적중률'])})")
    print(f"깨끗한 판의 지적 수: 견적서당 평균 {summary['깨끗한_판_지적_수_평균']:.1f}개")
    print("유형별 (찾음/심음): " + "  ".join(f"{t} {v['찾음']}/{v['심음']}" for t, v in summary["유형별"].items()))


# ── 실행 ──────────────────────────────────────────────────────────────────


class LocalParseCache:
    """이미지 파싱 결과를 파일에 두는 캐시. RiskParseCacheRepository와 같은 메서드를 갖는다.

    운영 DB의 캐시를 쓰지 않는 것은 시험용 이미지의 결과를 운영 컬렉션에 남기지 않기 위해서다.
    """

    def __init__(self, path=PARSE_CACHE_PATH):
        self._path = path
        self._data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    async def get_many(self, keys: list[str]) -> dict:
        from app.repositories.risk_parse_cache_repository import CachedParse
        return {k: CachedParse(**self._data[k]) for k in keys if k in self._data}

    async def put(self, key: str, parsed) -> None:
        self._data.setdefault(key, {"line_items": parsed.line_items, "input_tokens": parsed.input_tokens,
                                    "output_tokens": parsed.output_tokens})
        self._path.write_text(json.dumps(self._data, ensure_ascii=False), encoding="utf-8")


def load_records(split: str) -> list[dict]:
    """채점할 레코드. LLM 비교(eval/risk_llm_benchmark.py)도 이 함수를 쓴다 — 두 쪽이 다른 건을 채점하지 않게."""
    records = [r for r in json.loads(RISK_GT_PATH.read_text(encoding="utf-8"))
               if r["status"] == "verified" and r["split"] == split and r.get("planted")]
    if not records:
        raise SystemExit(f"결함을 심은 {split} 레코드가 없습니다 — python -m eval.risk_ground_truth build")
    return records


def region_text(info: dict) -> str:
    """리스크 진단은 지역을 자유 글로 받는다. 정답셋의 "수도권"은 지역 이름이 아니라서 경기로 넘긴다."""
    return "경기" if info["지역"] == "수도권" else info["지역"]


def require_final(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """평가용 세트 결과를 보면서 진단이나 프롬프트를 고치면 세트를 나눈 의미가 없어진다. 실수로 돌리지 않게 막는다."""
    if args.split != "dev" and not args.final:
        parser.error("평가용 세트는 마지막 비교 때만 실행합니다. 정말 실행하려면 --final")


async def run(split: str) -> None:
    from motor.motor_asyncio import AsyncIOMotorClient
    from sentence_transformers import CrossEncoder, SentenceTransformer

    from app.core.config import get_settings
    from app.domain.estimate_engine import EstimateEngine
    from app.domain.risk_detector_service import RiskDetectorService
    from app.repositories.coefficient_repository import CoefficientRepository
    from app.schemas.risk import AnalyzeRiskCommand
    from eval.quote_benchmark import LeaveOutCaseRepository

    records = load_records(split)
    # app/core/deps.py의 lifespan과 같은 구성 — 서비스와 다른 설정으로 채점하면 의미가 없다
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri, maxPoolSize=settings.mongo_max_pool_size,
                                serverSelectionTimeoutMS=settings.mongo_server_selection_timeout_ms)
    db = client[settings.mongo_db_name]
    repo = LeaveOutCaseRepository(db["estimate_cases"], settings)
    engine = EstimateEngine(
        case_repository=repo, embedder=SentenceTransformer(settings.embed_model),
        reranker=CrossEncoder(settings.reranker_model, max_length=512) if settings.use_reranker else None,
        vector_candidate_pool=settings.vector_candidate_pool,
        coefficients=await CoefficientRepository(collection=db["correction_coefficients"]).get_active(),
        window_includes_door=settings.estimate_window_includes_door,
    )
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    service = RiskDetectorService(engine, settings.risk_vision_max_concurrency,
                                  settings.risk_vision_max_concurrency_per_request, parse_cache=LocalParseCache())

    async def analyze(record: dict, doc: dict, path) -> list[dict]:
        info = doc["info"]
        report = await service.analyze(AnalyzeRiskCommand(
            space_type=info["공간유형"], pyeong=info["평수"], room_count=3, floor=info["층수"], elevator=info["엘리베이터"],
            region=region_text(info), building_age="10~20년", company_name="", image_files=[path.read_bytes()]))
        return muneo_findings(report)

    rows, details = [], []
    for record in records:
        await repo.leave_out(record["source"].get("request_url"), record["source"].get("article_id"))
        clean_path, planted_path = image_paths(record["id"])
        clean = await analyze(record, record, clean_path)
        planted = await analyze(record, record["planted_doc"], planted_path)
        row = {"id": record["id"], **score_record(record["planted"], clean, planted)}
        rows.append(row)
        details.append({**row, "clean": clean, "planted": planted, "new": new_findings(clean, planted)})
    client.close()

    summary = summarize(rows)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = RUNS_DIR / f"{split}_{stamp}.json"
    path.write_text(json.dumps({"split": split, "run_at": stamp, "summary": summary, "records": details},
                               ensure_ascii=False, indent=2), encoding="utf-8")
    print_report(rows, summary)
    print(f"\n[OK] {len(rows)}건 채점 → {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "eval"], default="dev")
    parser.add_argument("--final", action="store_true", help="평가용(eval) 세트 실행 확인 — 마지막 비교 때 한 번만")
    args = parser.parse_args()
    require_final(parser, args)
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(run(args.split))


if __name__ == "__main__":
    main()
