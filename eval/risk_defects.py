"""
eval/risk_defects.py — 깨끗한 견적서(품목 표)에 종류와 위치를 아는 결함을 심는다.

결함을 심는 함수는 모두 순수 함수다(품목 표를 받아 고친 사본과 "무엇을 어디에 심었는지"를 돌려준다). 어떤 유형을
어디에 심을지는 seed로 정해지므로 같은 견적서에는 언제나 같은 결함이 들어간다. 금액이 바뀌는 결함은 소계·공사비·
간접비·총액까지 맞춰 고친다 — 계산 오류(C1)만 일부러 소계를 틀리게 둔다.
"""

import copy
import random
import re

# 유형 → 채점에서 보는 갈래. 시스템의 지적이 이 갈래를 말하면 같은 종류의 문제를 가리킨 것으로 본다
KIND = {"M1": "누락", "M2": "누락", "D1": "중복", "D2": "중복", "U1": "불분명", "U2": "불분명", "U3": "불분명",
        "P1": "가격", "P2": "가격", "P3": "가격", "Q1": "수량", "C1": "계산"}
DEFECT_TYPES = list(KIND)
PRICE_FACTOR = {"P1": 2.0, "P2": 1.4, "P3": 0.4}
QUANTITY_FACTOR = 2.5
SUBTOTAL_ERROR = 0.10
N_DEFECTS = 3  # 견적서 하나에 심는 결함 수

# M1에서 지울 수 있는 줄: (섹션 이름에 든 낱말, 품명에 든 낱말). 공사가 성립하려면 있어야 하는 줄만 둔다
REQUIRED_LINES = [
    ("철거", ("폐기물",)), ("타일", ("방수",)), ("방수", ("방수",)), ("기타", ("방수",)),
    ("타일", ("부자재", "인건비")), ("도배", ("부자재", "인건비")), ("바닥", ("인건비",)),
    ("전기", ("인건비",)), ("조명", ("인건비",)), ("가구", ("상판", "인건비")),
]
CARRYING_WORDS = ("양중", "운반", "사다리차")
# 금액을 올리고 내려도 "시세"를 말할 수 없는 섹션(입력으로 표현할 수 없는 공사, 잡비)
NO_PRICE_SECTIONS = ("확장", "기타")
MEASURED_UNITS = ("평", "m2", "㎡", "자", "개", "장", "m")
_SPEC_RE = re.compile(r"\(.*?\)|/.*$|\d+\s*[*×xX]\s*\d+(\s*[*×xX]\s*\d+)?|\d+(\.\d+)?\s*(mm|T|t|W|인치)")


def _recompute(doc: dict, base_direct: int) -> None:
    """소계를 뺀 나머지 합계(공사비·간접비·총액)를 품목 표에 맞춘다. 간접비는 공사비에 비례해 다시 계산한다."""
    doc["direct"] = sum(s["subtotal"] for s in doc["sections"])
    ratio = doc["direct"] / base_direct if base_direct else 1.0
    indirect = {k: round(v * ratio) for k, v in doc["indirect"].items() if "단수" not in k}
    rounding = [k for k in doc["indirect"] if "단수" in k]
    if rounding:  # 원본처럼 만 원 아래를 버린다
        indirect[rounding[0]] = -((doc["direct"] + sum(indirect.values())) % 10_000)
    doc["indirect"] = indirect
    doc["total"] = doc["direct"] + sum(indirect.values())


def _resum(section: dict) -> None:
    section["subtotal"] = sum(line["amount"] or 0 for line in section["lines"])


def _strip_spec(desc: str) -> str:
    return re.sub(r"\s+", " ", _SPEC_RE.sub("", desc)).strip(" ,-:")


# ── 유형별: 심을 수 있는 자리 → 심기 ──────────────────────────────────────
# slots(doc)는 심을 수 있는 자리의 목록, apply(doc, slot)는 doc을 고치고 심은 내용을 돌려준다.


def _slots_m1(doc):
    out = []
    for si, sec in enumerate(doc["sections"]):
        if len(sec["lines"]) < 2:
            continue
        for sec_word, words in REQUIRED_LINES:
            if sec_word in sec["name"]:
                out += [(si, li) for li, line in enumerate(sec["lines"]) if any(w in line["desc"] for w in words)]
    return sorted(set(out))


def _apply_m1(doc, slot):
    si, li = slot
    sec = doc["sections"][si]
    line = sec["lines"].pop(li)
    _resum(sec)
    return {"section": sec["name"], "desc": line["desc"], "detail": f"'{line['desc']}' 줄({line['amount']:,}원)을 지움"}


def _slots_m2(doc):
    return [0]  # 공사 정보를 바꾸는 결함이라 자리는 하나다


def _apply_m2(doc, slot):
    removed = []
    for sec in doc["sections"]:
        keep = [line for line in sec["lines"] if not any(w in line["desc"] for w in CARRYING_WORDS)]
        removed += [line["desc"] for line in sec["lines"] if line not in keep]
        sec["lines"] = keep
        _resum(sec)
    doc["info"] = {**doc["info"], "층수": 9, "엘리베이터": False}
    return {"section": None, "desc": "양중/운반", "detail": f"9층·엘리베이터 없음, 양중·운반 줄 없음(지운 줄 {len(removed)}개)"}


def _slots_d1(doc):
    return [(si, li) for si, sec in enumerate(doc["sections"]) for li, line in enumerate(sec["lines"]) if line["amount"] >= 100_000]


def _apply_d1(doc, slot):
    si, li = slot
    sec = doc["sections"][si]
    line = sec["lines"][li]
    sec["lines"].insert(li + 1, dict(line))
    _resum(sec)
    return {"section": sec["name"], "desc": line["desc"], "detail": f"'{line['desc']}' 줄({line['amount']:,}원)을 한 번 더 넣음"}


def _slots_d2(doc):
    n = len(doc["sections"])
    return [(si, li, ti) for si, sec in enumerate(doc["sections"]) for li, line in enumerate(sec["lines"])
            if line["amount"] >= 100_000 for ti in range(n) if ti != si]


def _apply_d2(doc, slot):
    si, li, ti = slot
    line, target = doc["sections"][si]["lines"][li], doc["sections"][ti]
    target["lines"].append(dict(line))
    _resum(target)
    return {"section": target["name"], "also": doc["sections"][si]["name"], "desc": line["desc"],
            "detail": f"{doc['sections'][si]['name']}의 '{line['desc']}' 줄을 {target['name']}에도 넣음"}


def _slots_u1(doc):
    return [(si, li) for si, sec in enumerate(doc["sections"]) for li, line in enumerate(sec["lines"])
            if line["amount"] >= 300_000 and 2 <= len(_strip_spec(line["desc"])) <= len(line["desc"]) - 6]


def _apply_u1(doc, slot):
    si, li = slot
    line = doc["sections"][si]["lines"][li]
    before = line["desc"]
    line["desc"] = _strip_spec(before)
    return {"section": doc["sections"][si]["name"], "desc": line["desc"], "detail": f"'{before}' → '{line['desc']}' (제품명·규격을 지움)"}


def _slots_u2(doc):
    return [(si, li) for si, sec in enumerate(doc["sections"]) for li, line in enumerate(sec["lines"])
            if line["amount"] >= 200_000 and len(sec["lines"]) >= 2]


def _apply_u2(doc, slot):
    si, li = slot
    sec = doc["sections"][si]
    line = sec["lines"][li]
    was = line["amount"]
    line.update(desc=f"{line['desc']} (별도, 현장 협의)", unit_price=0, qty=0.0, amount=0)
    _resum(sec)
    return {"section": sec["name"], "desc": line["desc"], "detail": f"금액({was:,}원)을 지우고 '별도, 현장 협의'로 바꿈"}


def _slots_u3(doc):
    return [si for si, sec in enumerate(doc["sections"]) if len(sec["lines"]) >= 4]


def _apply_u3(doc, slot):
    sec = doc["sections"][slot]
    n = len(sec["lines"])
    name = sec["name"].replace(",", "·")
    sec["lines"] = [{"code": sec["lines"][0]["code"], "desc": f"{name} 일체", "unit_price": sec["subtotal"], "qty": 1.0,
                     "unit": "식", "amount": sec["subtotal"]}]
    return {"section": sec["name"], "desc": f"{name} 일체", "detail": f"{n}줄을 '{name} 일체 1식' 한 줄로 합침(금액 그대로)"}


def _slots_price(doc):
    return [si for si, sec in enumerate(doc["sections"])
            if sec["subtotal"] >= 1_000_000 and not any(w in sec["name"] for w in NO_PRICE_SECTIONS)]


def _apply_price(factor):
    def apply(doc, slot):
        sec = doc["sections"][slot]
        before = sec["subtotal"]
        for line in sec["lines"]:
            line["unit_price"] = round(line["unit_price"] * factor / 100) * 100
            line["amount"] = round(line["unit_price"] * line["qty"])
        _resum(sec)
        return {"section": sec["name"], "desc": sec["name"], "direction": "과다" if factor > 1 else "과소",
                "detail": f"{sec['name']}의 단가를 모두 {factor}배 ({before:,} → {sec['subtotal']:,}원)"}
    return apply


def _slots_q1(doc):
    return [(si, li) for si, sec in enumerate(doc["sections"]) for li, line in enumerate(sec["lines"])
            if line["qty"] >= 10 and line["unit"] in MEASURED_UNITS]


def _apply_q1(doc, slot):
    si, li = slot
    sec = doc["sections"][si]
    line = sec["lines"][li]
    before = line["qty"]
    line["qty"] = round(before * QUANTITY_FACTOR, 1)
    line["amount"] = round(line["unit_price"] * line["qty"])
    _resum(sec)
    return {"section": sec["name"], "desc": line["desc"], "direction": "과다",
            "detail": f"'{line['desc']}'의 수량을 {before:g} → {line['qty']:g}{line['unit']}로 늘림"}


def _slots_c1(doc):
    return [si for si, sec in enumerate(doc["sections"]) if sec["subtotal"] >= 1_000_000 and len(sec["lines"]) >= 2]


def _apply_c1(doc, slot):
    sec = doc["sections"][slot]
    real = sec["subtotal"]
    sec["subtotal"] = round(real * (1 + SUBTOTAL_ERROR) / 1000) * 1000
    return {"section": sec["name"], "desc": sec["name"], "detail": f"{sec['name']} 소계를 품목 합 {real:,} 대신 {sec['subtotal']:,}원으로 적음"}


_DEFECTS = {
    "M1": (_slots_m1, _apply_m1), "M2": (_slots_m2, _apply_m2), "D1": (_slots_d1, _apply_d1), "D2": (_slots_d2, _apply_d2),
    "U1": (_slots_u1, _apply_u1), "U2": (_slots_u2, _apply_u2), "U3": (_slots_u3, _apply_u3),
    "P1": (_slots_price, _apply_price(PRICE_FACTOR["P1"])), "P2": (_slots_price, _apply_price(PRICE_FACTOR["P2"])),
    "P3": (_slots_price, _apply_price(PRICE_FACTOR["P3"])), "Q1": (_slots_q1, _apply_q1), "C1": (_slots_c1, _apply_c1),
}


def _slot_sections(slot) -> set[int]:
    """자리가 건드리는 섹션. 한 섹션에는 결함을 하나만 심는다."""
    if isinstance(slot, tuple):
        return {slot[0]} | ({slot[2]} if len(slot) == 3 else set())
    return {slot}


def plant(clean: dict, types: list[str], rng: random.Random, n: int = N_DEFECTS) -> tuple[dict, list[dict]]:
    """깨끗한 판에 결함 n개를 심는다. types의 앞에서부터 시도하고, 심을 자리가 없는 유형은 건너뛴다.

    반환: (결함 판, 심은 결함 목록). 자리는 깨끗한 판을 기준으로 고른다(앞에서 심은 결함이 뒤의 자리를 바꾸지 않게).
    그래서 한 섹션에는 결함을 하나만 심는다.
    """
    doc = copy.deepcopy(clean)
    planted, used = [], set()
    for t in types:
        if len(planted) == n:
            break
        slots_fn, apply_fn = _DEFECTS[t]
        slots = [s for s in slots_fn(clean) if t == "M2" or not (_slot_sections(s) & used)]
        if not slots:
            continue
        slot = rng.choice(slots)
        if t != "M2":
            used |= _slot_sections(slot)
        planted.append({"type": t, "kind": KIND[t], **apply_fn(doc, slot)})
    _recompute(doc, clean["direct"])
    return doc, planted


def type_order(index: int, seed: int) -> list[str]:
    """index번째 견적서에 심을 유형의 순서. 앞의 N_DEFECTS개를 심고, 자리가 없으면 그 뒤의 유형으로 넘어간다.

    12개 유형을 seed로 한 번 섞은 순서를 견적서마다 N_DEFECTS칸씩 밀어 쓰므로, 전체에서 유형별 횟수가 고르다.
    """
    order = DEFECT_TYPES[:]
    random.Random(seed).shuffle(order)
    start = (index * N_DEFECTS) % len(order)
    return order[start:] + order[:start]
