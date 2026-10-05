"""
eval/quote_ground_truth.py — 가견적 벤치마크 정답셋(실제 견적서 → 입력 + 정답 금액) 작성 도구.

정답의 원천은 견적서 이미지의 "섹션 소계"(철거공사 2,132,000 / 창호공사 9,335,000 …)와 합계 행이다.
레코드의 quote 블록에 그 값을 옮겨 적으면, 공종별 정답·간접비·비교 총액·입력의 공종 목록은 이 스크립트가
규칙대로 계산한다. 코퍼스(estimate_cases)의 파싱 결과는 초안을 채우는 데만 쓴다 — 파일럿에서 문짝 품목
중복, 소계 행이 품목으로 들어온 경우, 옵션 행(금액 "-")이 품목이 된 경우가 나와 그대로는 정답이 못 된다.

    python -m eval.quote_ground_truth sample           # 후보 추출 → quote_gt_candidates.json
    python -m eval.quote_ground_truth draft --pilot    # 개선용(dev) 후보 중 앞 5건 초안 + 검수 시트
    python -m eval.quote_ground_truth draft --all      # 선정된 후보 전부 초안
    python -m eval.quote_ground_truth draft --ids 887507   # 글 번호 지정 (탈락한 건을 예비 후보로 대체할 때)
    python -m eval.quote_ground_truth draft --holdout  # 검증용(holdout) 세트 — 예비 후보에서 칸별 순서대로
    python -m eval.quote_ground_truth build            # quote 블록 → truth·input 재계산
    python -m eval.quote_ground_truth check            # 검산 2종 + 입력 스키마 검증

검수 순서: 검수 시트(estimate_data/_gt_review/)의 이미지를 열어 quote.sections·직접비_합계·간접비_내역·
총액_부가세제외·부가세_표기를 고친다 → status를 "verified"로 바꾼다 → build → check.
정답으로 쓸 수 없는 건(제목과 견적서 내용이 다른 경우 등)은 status를 "excluded"로 바꾸고 notes에 사유를 적는다.

정답으로 뽑은 견적은 코퍼스에 그대로 남는다. 채점할 때 같은 의뢰(request_url)의 사례를 검색에서
빼는 방식으로 누수를 막는다(같은 의뢰에 여러 업체 견적이 달리므로 게시글이 아니라 의뢰 단위).
"""

import argparse
import json
import pathlib
import random
import re
from collections import defaultdict

from dotenv import load_dotenv
from pydantic import ValidationError
from pymongo import MongoClient

from app.core.config import get_settings
from app.schemas.estimate import EstimateRequest
from pipeline.categories import normalize_category

load_dotenv()

RULES_VERSION = "1.1"  # 1.1: 도어를 창호·목공에서 떼어 별도 공종으로 (섹션의 "도어" 금액)
_DIR = pathlib.Path(__file__).parent / "test_inputs"
CANDIDATES_PATH = _DIR / "quote_gt_candidates.json"
GROUND_TRUTH_PATH = _DIR / "quote_ground_truth.json"
# 검수 시트는 품목 원문이 통째로 들어가므로 커밋하지 않는 위치(estimate_data/, 이미지 옆)에 둔다
REVIEW_DIR = pathlib.Path(__file__).parent.parent / "estimate_data" / "_gt_review"

REGION_BUCKET = {"서울": "서울", "경기": "수도권", "인천": "수도권"}
매핑_불가 = "매핑 불가"
미확인 = "(미확인)"
# 코퍼스 파싱 상태를 알려주는 초안 전용 플래그 — 검수가 끝난 레코드에는 남아 있으면 안 된다
DRAFT_ONLY_FLAGS = {"미분류_품목", "품목합_총액초과"}

# (지역, 평수대, 시공범위) 칸별로 새로 뽑을 건수. None은 "구분 없음".
# 수도권 30평대 전체는 기존 검증 사례(오산·화성) 2건이 있어 2건만 새로 뽑는다 → 합계 28 + 2 = 30.
QUOTA: list[tuple[tuple[str, str | None, str | None], int]] = [
    (("서울", "20평대 이하", "전체"), 5),
    (("서울", "30평대", "전체"), 5),
    (("서울", "40평 이상", "전체"), 2),
    (("서울", None, "부분"), 3),
    (("수도권", "20평대 이하", "전체"), 4),
    (("수도권", "30평대", "전체"), 2),
    (("수도권", "40평 이상", None), 2),
    (("수도권", None, "부분"), 5),
]

# 검증용(holdout) 세트. 평가용을 연 뒤에 고친 엔진을 검증하려고, 엔진도 LLM도 본 적 없는 견적으로 따로 만든다.
# 후보를 새로 뽑지 않고 이미 뽑아 둔 목록의 예비 후보를 칸별 순서대로 쓴다 — 순서는 sample의 seed로 정해져 있어
# 잘 맞을 것 같은 견적을 고를 수 없다. 칸별 건수는 평가용과 같다(정원에서 개선용 1건을 뺀 수).
HOLDOUT = "holdout"


def _cell_label(cell: tuple[str, str | None, str | None]) -> str:
    return "/".join(x or "전체범위" for x in cell)


HOLDOUT_QUOTA: dict[str, int] = {_cell_label(cell): n - 1 for cell, n in QUOTA}

# 초안용: 코퍼스의 정규화 카테고리 → (섹션 이름, 공종)
CATEGORY_TO_SECTION = {
    "철거": ("철거공사", "철거"), "확장": ("확장공사", 매핑_불가), "창호": ("창호공사", "창호"),
    "목공": ("목공사", "목공"), "타일": ("타일공사", "욕실"), "욕실": ("도기공사", "욕실"),
    "설비": ("수전공사", "욕실"), "도장": ("도장공사", "도장"), "전기": ("전기·조명공사", "전기/조명"),
    "가구": ("가구공사", "가구"), "도배": ("도배공사", "도배"), "필름": ("시트공사", "필름"),
    "공과잡비": ("기타공사", "마감/공과잡비"),
}
공종_순서 = ["창호", "도어", "욕실", "가구", "전기/조명", "도장", "필름", "장판", "마루", "도배", "목공", "철거", "마감/공과잡비"]
# 도어(방문·중문·현관문)가 들어 있을 수 있는 섹션의 공종. 이 섹션에는 "도어" 금액을 따로 적는다
도어_섹션_공종 = ("창호", "목공")
도어_미확인_플래그 = "도어_미확인"

_마루_RE = re.compile(r"마루|원목")
_장판_RE = re.compile(r"장판|모노륨|우드[름롬룸]|데코타일")
_샷시_RE = re.compile(r"샷시|샤시|새시|이중창|발코니창|확장창|시스템창")
_비욕실설비_RE = re.compile(r"보일러|분배기|난방|에어컨")
_거실바닥타일_RE = re.compile(r"거실.*타일")


def _collection():
    settings = get_settings()
    return MongoClient(settings.mongo_uri)[settings.mongo_db_name]["estimate_cases"]


def _size_band(size: int) -> str:
    return "20평대 이하" if size < 30 else ("30평대" if size < 40 else "40평 이상")


def _items(case: dict) -> list[dict]:
    return [i for i in (case.get("parsed_estimate") or {}).get("line_items", []) if int(i.get("amount") or 0) > 0]


def _sum(items: list[dict]) -> int:
    return sum(int(i.get("amount") or 0) for i in items)


def _draft_sections(case: dict) -> tuple[list[dict], dict[str, list[dict]]]:
    """코퍼스 품목을 섹션 단위로 묶은 초안. 반환: (sections, 섹션 이름별 품목)."""
    by_section: dict[str, list[dict]] = defaultdict(list)
    공종_of: dict[str, str] = {}
    for item in _items(case):
        cat = normalize_category(item.get("category", ""))
        if cat == "바닥":
            name = "바닥공사"  # 장판/마루는 아래에서 품목을 보고 정한다
        elif cat is None:
            name = "(미분류)"
            공종_of[name] = 매핑_불가
        else:
            name, 공종 = CATEGORY_TO_SECTION[cat]
            공종_of[name] = 공종
        by_section[name].append(item)
    if "바닥공사" in by_section:
        floor = by_section["바닥공사"]
        마루 = _sum([i for i in floor if _마루_RE.search(i.get("description") or "")])
        장판 = _sum([i for i in floor if _장판_RE.search(i.get("description") or "")])
        공종_of["바닥공사"] = "마루" if 마루 > 장판 else "장판"
    # 창호·목공 섹션의 "도어"는 초안에서 비워 둔다(None) — 검수할 때 이미지를 보고 적는다
    sections = [{"name": n, "amount": _sum(its), "공종": 공종_of[n],
                 **({"도어": None} if 공종_of[n] in 도어_섹션_공종 else {})} for n, its in by_section.items()]
    return sections, by_section


def _scope(공종들: list[str]) -> str:
    """도배·바닥·욕실이 모두 있고 실제 공종이 6개 이상이면 전체, 아니면 부분.

    초안 단계에서는 타일·도기·수전 섹션이 각각 '욕실'로 들어오므로 집합으로 바꿔 한 번만 센다.
    """
    # 도어는 창호와 한 공종으로 센다 — 도어를 떼기 전(규칙 1.0)과 판정이 달라지지 않게. 엔진의 사례 판정도 같다
    실공종 = {"창호" if g == "도어" else g for g in 공종들} - {매핑_불가, "마감/공과잡비"}
    바닥 = bool(실공종 & {"장판", "마루"})
    return "전체" if ({"도배", "욕실"} <= 실공종 and 바닥 and len(실공종) >= 6) else "부분"


def derive(record: dict) -> None:
    """quote 블록(견적서에서 옮긴 값)으로 truth와 input의 공종·시공범위·철거여부를 다시 계산한다."""
    q = record["quote"]
    공종별: dict[str, int] = defaultdict(int)
    unmapped = []
    door_unknown = False
    for s in q["sections"]:
        if s["amount"] <= 0:
            continue
        if s["공종"] == 매핑_불가:
            unmapped.append({"항목": s["name"], "금액": s["amount"]})
            continue
        # 섹션의 "도어"는 그 섹션 금액 중 도어(문짝·문틀·부속과 그 시공비)의 몫이다. 견적서가 도어를 창호공사에
        # 넣기도 하고 목공에 넣기도 해서, 섹션 단위로는 창호(샷시)·목공과 도어를 가를 수 없다.
        # None이면 확인하지 못한 것 — 떼지 않고 섹션의 공종에 둔다.
        door = s.get("도어") if s["공종"] in 도어_섹션_공종 else 0
        if door is None:
            door_unknown, door = True, 0
        if door:
            공종별["도어"] += door
        if s["amount"] - door > 0:
            공종별[s["공종"]] += s["amount"] - door
    공종별 = {g: 공종별[g] for g in 공종_순서 if g in 공종별}
    직접비, 총액 = q["직접비_합계"], q["총액_부가세제외"]
    매핑불가_합 = sum(m["금액"] for m in unmapped)

    record["truth"] = {
        "공종별": 공종별,
        "매핑_불가": unmapped,
        "직접비_합계": 직접비,
        "간접비": sum(q["간접비_내역"].values()),
        "총액_부가세제외": 총액,
        "부가세_표기": q["부가세_표기"],
        # 매핑 불가 항목은 입력으로 표현할 수 없으므로 그 항목에 붙은 간접비까지 비례해서 뺀다
        "비교_총액": round(총액 - 매핑불가_합 * 총액 / 직접비) if 직접비 else 총액,
        "비교_직접비": 직접비 - 매핑불가_합,
    }
    # 창호_도어만은 규칙 1.0의 플래그다. 도어를 떼면 그 견적은 공종이 "도어"뿐이라 플래그가 필요 없다
    flags = [f for f in record["flags"] if f not in ("매핑불가_10%초과", 도어_미확인_플래그)
             and not (f == "창호_도어만" and not door_unknown and "창호" not in 공종별)]
    if 직접비 and 매핑불가_합 > 직접비 * 0.10:
        flags.append("매핑불가_10%초과")
    if door_unknown:
        flags.append(도어_미확인_플래그)
    record["flags"] = flags

    inp = record["input"]
    inp["공종"] = list(공종별)
    inp["시공범위"] = _scope(inp["공종"])
    inp["철거여부"] = "있음" if "철거" in 공종별 else "없음"
    for opt in ("도배", "마루", "욕실"):
        if opt not in 공종별:
            inp.pop(opt, None)


# ── sample ────────────────────────────────────────────────────────────────


def sample(seed: int, force: bool) -> None:
    # 후보 목록은 한 번 뽑으면 고정이다. 다시 뽑으면 코퍼스 변화에 따라 목록이 달라져 이미 만든 정답의
    # 칸·개선용/평가용 구분과 어긋난다
    if CANDIDATES_PATH.exists() and not force:
        raise SystemExit(f"{CANDIDATES_PATH}가 이미 있습니다. 새로 뽑으려면 --force (기존 정답셋과 맞지 않게 됩니다)")
    cases = list(_collection().find(
        {"region": {"$in": list(REGION_BUCKET)}, "is_non_residential": {"$ne": True},
         "size_pyeong": {"$gt": 0, "$lt": 100}, "total_cost": {"$gt": 0},
         "request_url": {"$nin": [None, ""]}},
        {"embedding": 0, "body_text": 0, "request_body_text": 0},
    ))
    # 같은 의뢰에 견적이 여러 장(수정본, 다른 업체 견적)이면 글 번호가 가장 큰 것 하나만 남긴다
    latest: dict[str, dict] = {}
    for c in cases:
        if not _items(c) or not (c.get("local_images") or []):
            continue
        prev = latest.get(c["request_url"])
        if prev is None or int(c["article_id"]) > int(prev["article_id"]):
            latest[c["request_url"]] = c

    rng = random.Random(seed)
    pool = sorted(latest.values(), key=lambda c: c["article_id"])
    rng.shuffle(pool)

    picked_ids: set[str] = set()
    candidates = []
    for (region, band, scope), n in QUOTA:
        cell = []
        for c in pool:
            if c["article_id"] in picked_ids or REGION_BUCKET[c["region"]] != region:
                continue
            if band is not None and _size_band(int(c["size_pyeong"])) != band:
                continue
            # 칸을 나누는 시공범위는 코퍼스 파싱 기준의 근사값이다. 정답의 시공범위는 검수 후 derive()가 정한다
            공종들 = [s["공종"] for s in _draft_sections(c)[0]]
            if scope is not None and _scope(공종들) != scope:
                continue
            cell.append(c)
        label = _cell_label((region, band, scope))
        for i, c in enumerate(cell):
            picked_ids.add(c["article_id"])
            candidates.append({
                "article_id": c["article_id"],
                "request_url": c["request_url"],
                "cell": label,
                "region": c["region"],
                "size_pyeong": int(c["size_pyeong"]),
                # 칸마다 첫 번째만 개선용(dev) — 엔진을 고치면서 계속 봐도 되는 세트. 나머지는 평가용(eval)
                "split": "dev" if i == 0 else "eval",
                # 정원 안이면 selected, 넘으면 예비(검수에서 탈락한 건을 순서대로 대체)
                "role": "selected" if i < n else "reserve",
            })
        print(f"  {label}: 후보 {len(cell)}건 중 {min(n, len(cell))}건 선정")

    CANDIDATES_PATH.write_text(json.dumps(
        {"seed": seed, "rules_version": RULES_VERSION, "candidates": candidates},
        ensure_ascii=False, indent=2), encoding="utf-8")
    selected = [c for c in candidates if c["role"] == "selected"]
    print(f"[OK] 선정 {len(selected)}건 (dev {sum(1 for c in selected if c['split'] == 'dev')}, "
          f"eval {sum(1 for c in selected if c['split'] == 'eval')}), 예비 {len(candidates) - len(selected)}건 → {CANDIDATES_PATH}")


# ── draft ─────────────────────────────────────────────────────────────────


def _build_record(case: dict, cand: dict, seq: int) -> tuple[dict, str]:
    sections, by_section = _draft_sections(case)
    직접비 = _sum(_items(case))
    총액 = int(case.get("total_cost") or 0)

    flags = []
    if "(미분류)" in by_section:
        flags.append("미분류_품목")
    if 직접비 > 총액 * 1.02:
        flags.append("품목합_총액초과")  # 소계 행·중복 품목·옵션 행 의심 — 이미지의 섹션 소계로 확정
    창호 = by_section.get("창호공사", [])
    if 창호 and not any(_샷시_RE.search(i.get("description") or "") for i in 창호):
        flags.append("창호_도어만")
    욕실_items = by_section.get("타일공사", []) + by_section.get("도기공사", []) + by_section.get("수전공사", [])
    if any(_비욕실설비_RE.search(i.get("description") or "") for i in 욕실_items):
        flags.append("비욕실설비_포함")
    if any(_거실바닥타일_RE.search(i.get("description") or "") for i in 욕실_items):
        flags.append("거실바닥타일_포함")

    assumed = ["방개수", "자재등급", "건물연식", "층수", "엘리베이터", "트럭접근", "거주중공사", "공사시기"]
    inp: dict = {
        "공종": [], "시공범위": "부분", "공간유형": "아파트", "평수": int(case["size_pyeong"]), "방개수": 3,
        "지역": REGION_BUCKET[case["region"]], "건물연식": "10~20년", "자재등급": "중급", "철거여부": "없음",
        "층수": 1, "엘리베이터": "있음", "트럭접근": "가능", "거주중공사": "공실", "공사시기": "미정",
    }
    if "도배공사" in by_section:
        desc = " ".join(i.get("description") or "" for i in by_section["도배공사"])
        inp["도배"] = {"범위": "전체", "도배지종류": "합지벽지" if "합지" in desc and "실크" not in desc else "실크벽지"}
    if any(s["공종"] == "마루" for s in sections):
        inp["마루"] = {"범위": "전체"}
    if 욕실_items:
        변기 = sum(float(i.get("quantity") or 1) for i in 욕실_items if "양변" in (i.get("description") or ""))
        inp["욕실"] = {"개수": int(변기) if 1 <= 변기 <= 3 else 1}
        if not 1 <= 변기 <= 3:
            assumed.append("욕실.개수")

    record = {
        "id": f"gt-{seq:03d}",
        "status": "draft",
        "split": cand["split"],
        "rules_version": RULES_VERSION,
        "source": {"article_id": case["article_id"], "request_url": case["request_url"], "cell": cand["cell"]},
        "quote": {
            "sections": sections,
            "직접비_합계": 직접비,
            "간접비_내역": {미확인: 총액 - 직접비},
            "총액_부가세제외": 총액,
            "부가세_표기": "불명",
        },
        "input": inp,
        "assumed_fields": assumed,
        "truth": {},
        "flags": flags,
        "notes": "",
    }
    derive(record)

    lines = [
        f"# {record['id']} — article {case['article_id']} ({cand['cell']}, {cand['split']})",
        f"- 제목: {case.get('post_title')}",
        f"- 이미지: {', '.join(case.get('local_images') or [])}",
        f"- 의뢰글: {case['request_url']}",
        f"- 코퍼스 total_cost {총액:,} / 품목 합 {직접비:,} / flags: {flags or '없음'}",
        "", "## 의뢰글", (case.get("request_body_text") or "(없음)").strip()[:1200], "",
    ]
    for s in sections:
        lines.append(f"## {s['name']} → {s['공종']} — {s['amount']:,}원 (코퍼스 파싱 기준, 이미지 소계로 확인)")
        for i in by_section[s["name"]]:
            lines.append(f"- [{i.get('category')}] {i.get('code', '')} {i.get('description')} | "
                         f"{i.get('quantity', '')} {i.get('unit', '')} | {int(i['amount']):,}")
        lines.append("")
    return record, "\n".join(lines)


def _load() -> list[dict]:
    return json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8")) if GROUND_TRUTH_PATH.exists() else []


def _save(records: list[dict]) -> None:
    GROUND_TRUTH_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def holdout_targets(cands: list[dict], records: list[dict]) -> tuple[list[dict], dict[str, int]]:
    """검증용 세트로 새로 초안을 만들 후보와, 예비 후보가 모자란 칸(칸 → 모자란 건수).

    칸마다 정원에서 이미 있는 검증용 레코드(제외된 건은 빼고)를 뺀 만큼, 아직 레코드가 없는 예비 후보를 목록
    순서대로 고른다. 검수에서 탈락한 건을 excluded로 바꾸고 다시 실행하면 그 칸의 다음 후보가 채워진다.
    """
    done = {r["source"]["article_id"] for r in records}
    filled: dict[str, int] = defaultdict(int)
    for r in records:
        if r["split"] == HOLDOUT and r["status"] != "excluded":
            filled[r["source"]["cell"]] += 1
    targets, short = [], {}
    for label, quota in HOLDOUT_QUOTA.items():
        need = quota - filled[label]
        free = [c for c in cands if c["cell"] == label and c["role"] == "reserve" and c["article_id"] not in done]
        targets += [{**c, "split": HOLDOUT} for c in free[:max(need, 0)]]
        if need > len(free):
            short[label] = need - len(free)
    return targets, short


def draft(pilot: bool, ids: list[str] | None, holdout: bool = False) -> None:
    cands = json.loads(CANDIDATES_PATH.read_text(encoding="utf-8"))["candidates"]
    selected = [c for c in cands if c["role"] == "selected"]
    records = _load()
    if ids:
        # --holdout과 함께 주면 그 글을 검증용으로 만든다 (예비 후보가 모자란 칸을 다른 칸의 후보로 채울 때)
        targets = [{**c, "split": HOLDOUT} if holdout else c for c in cands if c["article_id"] in ids]
        for missing in sorted(set(ids) - {c["article_id"] for c in targets}):
            print(f"  [건너뜀] {missing}: 후보 목록에 없는 글 번호")
    elif holdout:
        targets, short = holdout_targets(cands, records)
        for label, n in short.items():
            print(f"  [부족] {label}: 예비 후보가 {n}건 모자람 — 다른 칸의 후보를 --holdout --ids로 지정")
    elif pilot:
        targets = [c for c in selected if c["split"] == "dev"][:5]
    else:
        targets = selected

    done = {r["source"]["article_id"] for r in records}
    col = _collection()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    seq = max((int(r["id"].split("-")[1]) for r in records), default=0)
    for cand in targets:
        if cand["article_id"] in done:
            continue  # 이미 만든(검수했을 수 있는) 레코드는 덮어쓰지 않는다
        case = col.find_one({"article_id": cand["article_id"]}, {"embedding": 0, "body_text": 0})
        if case is None:
            print(f"  [건너뜀] {cand['article_id']}: 코퍼스에 없음")
            continue
        seq += 1
        record, sheet = _build_record(case, cand, seq)
        records.append(record)
        (REVIEW_DIR / f"{record['id']}_{cand['article_id']}.md").write_text(sheet, encoding="utf-8")
        print(f"  {record['id']} article {cand['article_id']} {cand['cell']} flags={record['flags'] or '없음'}")
    _save(records)
    print(f"[OK] 레코드 {len(records)}건 → {GROUND_TRUTH_PATH}\n     검수 시트 → {REVIEW_DIR}")


# ── build / check ─────────────────────────────────────────────────────────


def build() -> None:
    records = _load()
    for r in records:
        if r["status"] != "excluded":
            derive(r)
    _save(records)
    print(f"[OK] {len(records)}건 truth·input 재계산")


def check_record(record: dict) -> list[str]:
    """검산 2종과 입력 스키마 검증. 문제 목록을 반환한다(없으면 빈 리스트)."""
    q = record["quote"]
    problems = []
    # derive()는 공종_순서에 없는 공종을 정답에 넣지 않는다. 오타가 조용히 빠지지 않게 여기서 막는다
    unknown = sorted({s["공종"] for s in q["sections"]} - set(공종_순서) - {매핑_불가})
    if unknown:
        problems.append(f"알 수 없는 공종: {unknown}")
    for s in q["sections"]:
        if s["공종"] not in 도어_섹션_공종:
            continue
        if "도어" not in s:
            problems.append(f"{s['name']}: 도어 금액이 없음 (없으면 0, 확인 못 했으면 null)")
        elif s["도어"] is not None and not 0 <= s["도어"] <= s["amount"]:
            problems.append(f"{s['name']}: 도어 금액({s['도어']:,})이 섹션 금액({s['amount']:,})을 벗어남")
    섹션합 = sum(s["amount"] for s in q["sections"])
    if 섹션합 != q["직접비_합계"]:
        problems.append(f"섹션 합({섹션합:,}) ≠ 직접비 합계({q['직접비_합계']:,})")
    if q["직접비_합계"] + sum(q["간접비_내역"].values()) != q["총액_부가세제외"]:
        problems.append("직접비 + 간접비 ≠ 부가세 제외 총액")
    # 초안의 간접비는 '코퍼스 총액 − 품목 합'이라, 음수면 품목이 중복됐다는 뜻이다. 견적서에서 옮긴 값은
    # 할인으로 음수가 될 수 있으므로 초안일 때만 본다
    if 미확인 in q["간접비_내역"] and q["간접비_내역"][미확인] < 0:
        problems.append(f"품목 합이 코퍼스 총액보다 큼({-q['간접비_내역'][미확인]:,}원)")
    expected = json.loads(json.dumps(record))
    derive(expected)
    if expected["truth"] != record["truth"] or expected["input"] != record["input"]:
        problems.append("truth·input이 quote와 어긋남 — build를 다시 실행")
    if record["status"] == "verified":
        if 미확인 in q["간접비_내역"] or q["부가세_표기"] == "불명":
            problems.append("verified인데 간접비 내역 또는 부가세 표기가 미확인")
        leftover = sorted(DRAFT_ONLY_FLAGS & set(record["flags"]))
        if leftover:
            problems.append(f"verified인데 초안 전용 플래그가 남아 있음: {leftover}")
    try:
        EstimateRequest(**record["input"])
    except ValidationError as exc:
        problems.append(f"입력 스키마 오류: {exc.errors()[0]['loc']} {exc.errors()[0]['msg']}")
    return problems


def check() -> None:
    records = _load()
    bad = 0
    for r in records:
        if r["status"] == "excluded":  # 검수에서 탈락한 건 — 사유는 notes. 예비 후보로 대체한다
            print(f"  {r['id']} [excluded/{r['split']}] {r['notes']}")
            continue
        problems = check_record(r)
        bad += bool(problems)
        print(f"  {r['id']} [{r['status']}/{r['split']}] " + ("OK" if not problems else " / ".join(problems)))
    verified = sum(1 for r in records if r["status"] == "verified")
    excluded = sum(1 for r in records if r["status"] == "excluded")
    print(f"[{'OK' if not bad else 'FAIL'}] {len(records)}건 중 문제 {bad}건, 검수 완료 {verified}건, 제외 {excluded}건")
    if bad:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--seed", type=int, default=67)
    p.add_argument("--force", action="store_true", help="기존 후보 목록을 덮어쓴다")
    p = sub.add_parser("draft")
    p.add_argument("--pilot", action="store_true", help="개선용(dev) 후보 중 앞 5건만")
    p.add_argument("--all", action="store_true", help="선정된 후보 전부")
    p.add_argument("--ids", nargs="*", help="article_id 지정 (예비 후보 포함)")
    p.add_argument("--holdout", action="store_true", help="검증용(holdout) 세트 — 예비 후보에서 칸별 순서대로")
    sub.add_parser("build")
    sub.add_parser("check")
    args = parser.parse_args()

    if args.cmd == "sample":
        sample(args.seed, args.force)
    elif args.cmd == "draft":
        if not (args.pilot or args.all or args.ids or args.holdout):
            parser.error("--pilot, --all, --ids, --holdout 중 하나를 지정하세요")
        draft(args.pilot, args.ids, args.holdout)
    elif args.cmd == "build":
        build()
    else:
        check()


if __name__ == "__main__":
    main()
