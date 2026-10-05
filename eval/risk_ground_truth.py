"""
eval/risk_ground_truth.py — 리스크 진단 벤치마크 정답셋(깨끗한 견적서 + 결함을 심은 견적서) 작성 도구.

리스크는 견적서에 답이 적혀 있지 않다. 그래서 검수를 마친 실제 견적서를 품목 표로 옮겨 "깨끗한 판"을 만들고,
거기에 종류와 위치를 아는 결함을 심어 "결함 판"을 만든다. 채점은 심은 결함을 찾았는지와, 깨끗한 판에 지적을
몇 개 했는지를 본다(eval/risk_benchmark.py).

    python -m eval.risk_ground_truth sample    # 바탕 견적서 선정 → risk_ground_truth.json (한 번만)
    python -m eval.risk_ground_truth base      # 코퍼스 품목으로 품목 표 초안
    python -m eval.risk_ground_truth replace   # 제외(excluded)한 바탕 견적서를 다음 후보로 대체
    python -m eval.risk_ground_truth check     # 검산: 섹션 합 = 검수한 소계, 단가 × 수량 = 금액
    python -m eval.risk_ground_truth build     # 결함을 심고, 깨끗한 판과 결함 판 이미지를 그린다

바탕 견적서는 가견적 정답셋에서 검수를 마친 전체 시공 견적서다. 섹션 소계와 공사비를 원본 이미지에서 원 단위로
확인해 둔 것이라, 품목 표가 맞게 옮겨졌는지를 그 소계로 검산할 수 있다. 코퍼스의 품목은 초안으로만 쓴다 —
같은 줄이 두 번 읽히거나 금액이 틀린 줄이 있어, 소계가 안 맞는 섹션은 원본 이미지를 보고 고친다.
"""

import argparse
import json
import pathlib
import random
from collections import defaultdict

from dotenv import load_dotenv
from pymongo import MongoClient

from app.core.config import get_settings
from eval.quote_ground_truth import GROUND_TRUTH_PATH as QUOTE_GT_PATH
from eval.risk_defects import plant, type_order

load_dotenv()

RULES_VERSION = "1.1"
RISK_GT_PATH = pathlib.Path(__file__).parent / "test_inputs" / "risk_ground_truth.json"
# 검수 시트와 만든 이미지는 품목 원문이 통째로 들어가므로 커밋하지 않는 위치에 둔다
RISK_DIR = pathlib.Path(__file__).parent.parent / "estimate_data" / "_risk_gt"

N_BASE = 20  # 바탕 견적서 수
N_DEV = 6    # 그중 개선용. 리스크 진단을 고치는 동안에는 개선용만 본다


def _load() -> list[dict]:
    return json.loads(RISK_GT_PATH.read_text(encoding="utf-8")) if RISK_GT_PATH.exists() else []


def _save(records: list[dict]) -> None:
    RISK_GT_PATH.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _collection():
    settings = get_settings()
    return MongoClient(settings.mongo_uri)[settings.mongo_db_name]["estimate_cases"]


# ── sample ────────────────────────────────────────────────────────────────


def base_pool(quote_records: list[dict], seed: int) -> list[dict]:
    """바탕 견적서 후보를 seed로 섞은 순서. 검수를 마친 전체 시공 견적서만 쓴다.

    전체 시공만 쓰는 것은 결함을 심을 자리(공종과 품목)가 많아서다. 원본 이미지가 없는 건(출처에 글 번호가 없는 건)은
    품목 표를 확인할 수 없어 뺀다. 어떤 견적서가 시스템에 유리한지는 고르는 데 쓰지 않는다.
    """
    pool = [r for r in quote_records
            if r["status"] == "verified" and r["input"]["시공범위"] == "전체" and r["source"].get("article_id")]
    pool.sort(key=lambda r: r["id"])
    random.Random(seed).shuffle(pool)
    return pool


def pick_bases(quote_records: list[dict], seed: int) -> list[dict]:
    """바탕 견적서를 고른다. 섞은 순서의 앞에서부터 N_BASE건, 그중 앞 N_DEV건이 개선용."""
    return [{"quote_id": r["id"], "split": "dev" if i < N_DEV else "eval"}
            for i, r in enumerate(base_pool(quote_records, seed)[:N_BASE])]


def next_bases(quote_records: list[dict], records: list[dict]) -> list[dict]:
    """제외한 바탕 견적서를 대신할 후보. 제외한 건마다 같은 세트(개선용/평가용)로, 섞은 순서의 다음 후보를 준다.

    제외 사유는 견적서 자체의 문제(요약표가 스스로 맞지 않음 등)여야 하고 notes에 적는다. 시스템 출력은 사유가 못 된다.
    """
    if not records:
        return []
    used = {r["source"]["quote_id"] for r in records}
    free = [q for q in base_pool(quote_records, records[0]["seed"]) if q["id"] not in used]
    need: dict[str, int] = defaultdict(int)
    for r in records:
        need[r["split"]] += r["status"] == "excluded"
        need[r["split"]] -= bool(r.get("replaces"))  # 이미 대체한 만큼은 뺀다
    out = []
    for split in ("dev", "eval"):
        for _ in range(max(need[split], 0)):
            if free:
                out.append({"quote_id": free.pop(0)["id"], "split": split})
    return out


def sample(seed: int, force: bool) -> None:
    if RISK_GT_PATH.exists() and not force:
        raise SystemExit(f"{RISK_GT_PATH}가 이미 있습니다. 새로 뽑으려면 --force (만든 정답이 지워집니다)")
    quote_records = json.loads(QUOTE_GT_PATH.read_text(encoding="utf-8"))
    picked = pick_bases(quote_records, seed)
    by_id = {r["id"]: r for r in quote_records}
    records = [_new_record(i, by_id[p["quote_id"]], p["split"], seed) for i, p in enumerate(picked, 1)]
    _save(records)
    print(f"[OK] 바탕 견적서 {len(records)}건 (개선용 {N_DEV}, 평가용 {len(records) - N_DEV}) → {RISK_GT_PATH}")


def _new_record(seq: int, q: dict, split: str, seed: int) -> dict:
    return {
        "id": f"rk-{seq:03d}", "status": "draft", "split": split, "rules_version": RULES_VERSION, "seed": seed,
        "source": {"quote_id": q["id"], "article_id": q["source"]["article_id"], "request_url": q["source"].get("request_url")},
        # 층수·엘리베이터는 견적서에 없다. 양중 비용이 문제 되지 않는 값으로 두고, 조건 누락 결함(M2)만 이 값을 바꾼다
        "info": {"평수": q["input"]["평수"], "지역": q["input"]["지역"], "공간유형": q["input"]["공간유형"],
                 "층수": 3, "엘리베이터": True},
        "sections": [], "indirect": {}, "total": 0, "notes": "",
    }


def replace() -> None:
    records = _load()
    quote_records = json.loads(QUOTE_GT_PATH.read_text(encoding="utf-8"))
    by_id = {r["id"]: r for r in quote_records}
    added = next_bases(quote_records, records)
    for p in added:
        record = _new_record(len(records) + 1, by_id[p["quote_id"]], p["split"], records[0]["seed"])
        record["replaces"] = True
        records.append(record)
        print(f"  {record['id']} ({p['split']}, {p['quote_id']}) 추가")
    _save(records)
    print(f"[OK] 대체 {len(added)}건 — 이어서 base를 실행")


# ── base ──────────────────────────────────────────────────────────────────


def _code_group(code) -> str | None:
    """품목 코드의 섹션 번호. 이 양식의 코드는 섹션 번호 × 100 + 줄 번호다(1501 → 15)."""
    digits = "".join(ch for ch in str(code or "") if ch.isdigit())
    return digits[:-2] if len(digits) > 2 else None


def group_items(items: list[dict]) -> list[list[dict]]:
    """품목을 나온 순서대로 섹션 단위로 묶는다. 코드의 섹션 번호가 바뀌면 새 묶음이고, 코드가 없는 줄은 앞 줄을 따른다."""
    groups: list[list[dict]] = []
    current = None
    for item in items:
        group = _code_group(item.get("code"))
        if group is None:
            group = current
        if not groups or group != current:
            groups.append([])
        current = group
        groups[-1].append(item)
    return groups


def _line(item: dict) -> dict:
    return {"code": str(item.get("code") or ""), "desc": str(item.get("description") or "").strip(),
            "unit_price": int(item.get("unit_price") or 0), "qty": float(item.get("quantity") or 0),
            "unit": str(item.get("unit") or ""), "amount": int(item.get("amount") or 0)}


def build_sections(quote_sections: list[dict], items: list[dict]) -> list[dict]:
    """검수한 섹션(이름·소계)에 코퍼스 품목 묶음을 순서대로 대응시킨 초안.

    묶음의 합이 소계와 같은 섹션부터 짝짓고, 남은 섹션과 남은 묶음은 순서대로 짝짓는다. 소계가 안 맞는 섹션은
    check가 잡아내며, 원본 이미지를 보고 lines를 고친다.
    """
    groups = [[_line(i) for i in g if int(i.get("amount") or 0) > 0] for g in group_items(items)]
    groups = [g for g in groups if g]
    sections = [{"name": s["name"], "subtotal": s["amount"], "lines": None} for s in quote_sections]
    used: set[int] = set()
    for sec in sections:
        for gi, g in enumerate(groups):
            if gi not in used and sum(line["amount"] for line in g) == sec["subtotal"]:
                sec["lines"] = g
                used.add(gi)
                break
    rest = [g for gi, g in enumerate(groups) if gi not in used]
    for sec in sections:
        if sec["lines"] is None:
            sec["lines"] = rest.pop(0) if rest else []
    return sections


def base() -> None:
    records = _load()
    quotes = {r["id"]: r for r in json.loads(QUOTE_GT_PATH.read_text(encoding="utf-8"))}
    col = _collection()
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    for r in records:
        if r["sections"] or r["status"] == "excluded":
            continue  # 이미 만든(고쳤을 수 있는) 품목 표는 덮어쓰지 않는다
        q = quotes[r["source"]["quote_id"]]["quote"]
        case = col.find_one({"article_id": r["source"]["article_id"]}, {"embedding": 0, "body_text": 0})
        items = (case.get("parsed_estimate") or {}).get("line_items", []) if case else []
        r["sections"] = build_sections(q["sections"], items)
        r["direct"] = q["직접비_합계"]
        r["indirect"] = dict(q["간접비_내역"])
        r["total"] = q["총액_부가세제외"]
        problems = check_record(r)
        print(f"  {r['id']} ({r['split']}, {r['source']['quote_id']}) 섹션 {len(r['sections'])}개, "
              f"품목 {sum(len(s['lines']) for s in r['sections'])}줄 — 문제 {len(problems)}건")
    _save(records)
    print(f"[OK] {len(records)}건 → {RISK_GT_PATH}")


# ── check ─────────────────────────────────────────────────────────────────


def check_record(record: dict) -> list[str]:
    """깨끗한 판의 검산. 문제 목록을 반환한다(없으면 빈 리스트)."""
    problems = []
    for sec in record["sections"]:
        got = sum(line["amount"] for line in sec["lines"])
        if got != sec["subtotal"]:
            problems.append(f"{sec['name']}: 품목 합 {got:,} ≠ 소계 {sec['subtotal']:,} (차이 {got - sec['subtotal']:+,})")
        seen: dict[tuple, int] = defaultdict(int)
        for line in sec["lines"]:
            # 단가 × 수량이 금액과 다르면 깨끗한 판에 계산 오류가 있는 것이다
            if round(line["unit_price"] * line["qty"]) != line["amount"]:
                problems.append(f"{sec['name']} / {line['desc'][:20]}: 단가 {line['unit_price']:,} × 수량 {line['qty']:g} ≠ 금액 {line['amount']:,}")
            seen[(line["desc"], line["amount"])] += 1
        # 같은 섹션에 품명과 금액이 같은 줄이 둘이면 깨끗한 판에 중복이 있는 것이다
        problems += [f"{sec['name']} / {desc[:20]}: 같은 줄이 {n}번" for (desc, _), n in seen.items() if n > 1]
    if sum(sec["subtotal"] for sec in record["sections"]) != record.get("direct"):
        problems.append("섹션 소계의 합 ≠ 공사비")
    if record.get("direct", 0) + sum(record["indirect"].values()) != record["total"]:
        problems.append("공사비 + 간접비 ≠ 총액")
    return problems


def check(split: str | None) -> None:
    bad = 0
    records = [r for r in _load() if split is None or r["split"] == split]
    for r in records:
        if r["status"] == "excluded":
            print(f"  {r['id']} [excluded/{r['split']}] {r['notes']}")
            continue
        problems = check_record(r)
        bad += bool(problems)
        print(f"  {r['id']} [{r['status']}/{r['split']}] " + ("OK" if not problems else f"문제 {len(problems)}건"))
        for p in problems:
            print(f"      {p}")
    print(f"[{'OK' if not bad else 'FAIL'}] {len(records)}건 중 문제 있는 건 {bad}건")
    if bad:
        raise SystemExit(1)


# ── build ─────────────────────────────────────────────────────────────────


def clean_doc(record: dict) -> dict:
    """레코드에서 깨끗한 판(그릴 수 있는 품목 표)만 꺼낸다."""
    return {k: record[k] for k in ("info", "sections", "direct", "indirect", "total")}


def image_paths(record_id: str) -> tuple[pathlib.Path, pathlib.Path]:
    return RISK_DIR / f"{record_id}_clean.png", RISK_DIR / f"{record_id}_planted.png"


def build() -> None:
    from eval.risk_render import render  # 글꼴이 있어야 해서 그릴 때만 가져온다

    records = _load()
    RISK_DIR.mkdir(parents=True, exist_ok=True)
    usable = [r for r in records if r["status"] != "excluded"]
    for index, r in enumerate(usable):
        if r["status"] != "verified":
            continue  # 원본과 대조를 마친 견적서에만 심는다. 순번은 건너뛰지 않아 유형이 고르게 돌아간다
        if check_record(r):
            raise SystemExit(f"{r['id']}: 검산이 안 맞는 견적서에는 결함을 심지 않는다 — check를 먼저 통과시킬 것")
        clean = clean_doc(r)
        rng = random.Random(f"{r['seed']}-{r['id']}")
        r["planted_doc"], r["planted"] = plant(clean, type_order(index, r["seed"]), rng)
        clean_path, planted_path = image_paths(r["id"])
        render(clean, clean_path)
        render(r["planted_doc"], planted_path)
        print(f"  {r['id']} ({r['split']}): " + " / ".join(f"{p['type']} {p['section'] or '공통'}" for p in r["planted"]))
    _save(records)
    print(f"[OK] 이미지 → {RISK_DIR}")


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("sample")
    p.add_argument("--seed", type=int, default=89)
    p.add_argument("--force", action="store_true", help="기존 정답 파일을 덮어쓴다")
    sub.add_parser("base")
    sub.add_parser("replace")
    sub.add_parser("build")
    p = sub.add_parser("check")
    p.add_argument("--split", choices=["dev", "eval"])
    args = parser.parse_args()
    if args.cmd == "sample":
        sample(args.seed, args.force)
    elif args.cmd == "base":
        base()
    elif args.cmd == "replace":
        replace()
    elif args.cmd == "build":
        build()
    else:
        check(args.split)


if __name__ == "__main__":
    main()
