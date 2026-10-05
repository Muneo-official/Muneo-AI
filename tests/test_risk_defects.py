"""
리스크 벤치마크의 결함 심기 단위테스트 — 심은 결함이 정답이므로, 심는 코드가 틀리면 채점 전체가 틀린다.

금액은 지어낸 값이다.
"""

import copy
import random

import pytest

from eval.risk_defects import DEFECT_TYPES, KIND, N_DEFECTS, plant, type_order
from eval.risk_ground_truth import build_sections, check_record, group_items


def _line(code, desc, unit_price, qty, unit="식"):
    return {"code": code, "desc": desc, "unit_price": unit_price, "qty": float(qty), "unit": unit, "amount": round(unit_price * qty)}


def _doc() -> dict:
    sections = [
        {"name": "철거공사", "lines": [_line("101", "철거 인건비", 250_000, 4, "인"), _line("102", "폐기물 처리비", 350_000, 2, "대")]},
        {"name": "타일공사", "lines": [_line("201", "화장실 벽타일(300*600)/포세린", 25_000, 20, "m2"), _line("202", "방수", 400_000, 1),
                                   _line("203", "부자재(시멘트, 본드)", 200_000, 1), _line("204", "인건비", 350_000, 3, "M/D")]},
        {"name": "가구공사", "lines": [_line("301", "싱크 하부장(Pet)/인조대리석/E0", 150_000, 15, "자"), _line("302", "인건비", 300_000, 1, "M/D")]},
        {"name": "도배공사", "lines": [_line("401", "실크벽지(LX 베스띠)", 11_000, 90, "평"), _line("402", "부자재(풀, 부직포)", 300_000, 1),
                                   _line("403", "인건비", 280_000, 5, "M/D")]},
        {"name": "기타공사", "lines": [_line("501", "자재 양중", 200_000, 1), _line("502", "승강기 보양", 150_000, 1)]},
    ]
    for s in sections:
        s["subtotal"] = sum(line["amount"] for line in s["lines"])
    direct = sum(s["subtotal"] for s in sections)
    profit = round(direct * 0.07)
    rounding = -((direct + profit) % 10_000)
    return {"info": {"평수": 30, "지역": "서울", "공간유형": "아파트", "층수": 3, "엘리베이터": True}, "sections": sections,
            "direct": direct, "indirect": {"이윤(공사비×7%)": profit, "단수": rounding}, "total": direct + profit + rounding}


def _plant(defect_type: str, seed: int = 1) -> tuple[dict, dict, dict]:
    clean = _doc()
    doc, planted = plant(clean, [defect_type], random.Random(seed), n=1)
    assert len(planted) == 1 and planted[0]["type"] == defect_type and planted[0]["kind"] == KIND[defect_type]
    return clean, doc, planted[0]


def _section(doc: dict, name: str) -> dict:
    return next(s for s in doc["sections"] if s["name"] == name)


def _consistent(doc: dict) -> bool:
    """소계 = 품목 합, 공사비 = 소계 합, 총액 = 공사비 + 간접비."""
    return (all(sum(line["amount"] for line in s["lines"]) == s["subtotal"] for s in doc["sections"])
            and sum(s["subtotal"] for s in doc["sections"]) == doc["direct"]
            and doc["direct"] + sum(doc["indirect"].values()) == doc["total"])


def test_깨끗한_판은_바뀌지_않는다():
    clean = _doc()
    before = copy.deepcopy(clean)
    plant(clean, DEFECT_TYPES, random.Random(3))
    assert clean == before


@pytest.mark.parametrize("defect_type", [t for t in DEFECT_TYPES if t != "C1"])
def test_계산_오류를_빼면_결함_판의_합계는_스스로_맞는다(defect_type):
    _, doc, _ = _plant(defect_type)
    assert _consistent(doc)
    assert doc["total"] % 10_000 == 0  # 단수는 원본처럼 만 원 아래를 버린다


def test_필수_항목_누락은_표에_있는_줄만_지운다():
    for seed in range(20):
        clean, doc, planted = _plant("M1", seed)
        sec = _section(doc, planted["section"])
        assert len(sec["lines"]) == len(_section(clean, planted["section"])["lines"]) - 1
        assert any(w in planted["desc"] for w in ("폐기물", "방수", "부자재", "인건비", "상판"))
        assert planted["section"] != "철거공사" or "폐기물" in planted["desc"]  # 철거에서는 폐기물 처리만 지운다


def test_조건_누락은_공사_정보를_고층으로_바꾸고_양중_줄을_지운다():
    _, doc, planted = _plant("M2")
    assert doc["info"]["층수"] >= 5 and doc["info"]["엘리베이터"] is False
    assert not any("양중" in line["desc"] for s in doc["sections"] for line in s["lines"])
    assert planted["section"] is None


def test_중복은_같은_줄을_한_번_더_넣는다():
    clean, doc, planted = _plant("D1")
    descs = [line["desc"] for line in _section(doc, planted["section"])["lines"]]
    assert descs.count(planted["desc"]) == 2
    assert doc["direct"] > clean["direct"]


def test_다른_공종_중복은_원래_줄을_두고_다른_섹션에_넣는다():
    _, doc, planted = _plant("D2")
    assert planted["section"] != planted["also"]
    assert any(line["desc"] == planted["desc"] for line in _section(doc, planted["also"])["lines"])
    assert any(line["desc"] == planted["desc"] for line in _section(doc, planted["section"])["lines"])


def test_사양_불분명은_제품명과_규격만_지우고_금액은_그대로다():
    clean, doc, planted = _plant("U1")
    assert doc["total"] == clean["total"]
    assert "(" not in planted["desc"] and "/" not in planted["desc"]


def test_범위_불분명은_금액을_지우고_별도로_적는다():
    clean, doc, planted = _plant("U2")
    line = next(line for line in _section(doc, planted["section"])["lines"] if line["desc"] == planted["desc"])
    assert line["amount"] == 0 and "별도" in line["desc"]
    assert doc["direct"] < clean["direct"]


def test_일괄_처리는_한_줄로_합치고_금액은_그대로다():
    clean, doc, planted = _plant("U3")
    sec = _section(doc, planted["section"])
    assert len(sec["lines"]) == 1 and sec["lines"][0]["unit"] == "식"
    assert sec["subtotal"] == _section(clean, planted["section"])["subtotal"] and doc["total"] == clean["total"]


@pytest.mark.parametrize(("defect_type", "factor", "direction"), [("P1", 2.0, "과다"), ("P2", 1.4, "과다"), ("P3", 0.4, "과소")])
def test_가격_결함은_한_섹션의_금액을_배수로_바꾼다(defect_type, factor, direction):
    clean, doc, planted = _plant(defect_type)
    before, after = _section(clean, planted["section"])["subtotal"], _section(doc, planted["section"])["subtotal"]
    assert after == pytest.approx(before * factor, rel=0.01)
    assert planted["direction"] == direction
    assert "기타" not in planted["section"]  # 잡비 섹션에는 시세를 말할 수 없다


def test_수량_과다는_재는_단위의_줄만_늘린다():
    _, doc, planted = _plant("Q1")
    line = next(line for line in _section(doc, planted["section"])["lines"] if line["desc"] == planted["desc"])
    assert line["unit"] in ("평", "m2", "자") and line["amount"] == round(line["unit_price"] * line["qty"])


def test_계산_오류는_소계만_틀리게_적고_품목은_그대로다():
    clean, doc, planted = _plant("C1")
    sec = _section(doc, planted["section"])
    assert sec["lines"] == _section(clean, planted["section"])["lines"]
    assert sec["subtotal"] > sum(line["amount"] for line in sec["lines"])
    assert doc["direct"] == sum(s["subtotal"] for s in doc["sections"])  # 틀린 소계가 공사비·총액까지 이어진다


def test_한_섹션에는_결함을_하나만_심는다():
    for seed in range(30):
        _, planted = plant(_doc(), [t for t in DEFECT_TYPES if t != "M2"], random.Random(seed))
        touched = [p["section"] for p in planted] + [p["also"] for p in planted if "also" in p]
        assert len(planted) == N_DEFECTS and len(touched) == len(set(touched))


def test_같은_seed면_같은_결함이_심긴다():
    a = plant(_doc(), DEFECT_TYPES, random.Random("89-rk-001"))
    b = plant(_doc(), DEFECT_TYPES, random.Random("89-rk-001"))
    assert a == b


def test_유형은_견적서들에_고르게_돌아간다():
    counts = {t: 0 for t in DEFECT_TYPES}
    for index in range(20):
        for t in type_order(index, seed=89)[:N_DEFECTS]:
            counts[t] += 1
    assert set(counts.values()) == {5}


# ── 품목 표 초안과 검산 ────────────────────────────────────────────────────


def test_품목은_코드의_섹션_번호로_묶고_코드가_없는_줄은_앞_줄을_따른다():
    items = [{"code": "101"}, {"code": ""}, {"code": "205"}, {"code": "1501"}, {"code": None}]
    assert [len(g) for g in group_items(items)] == [2, 1, 2]


def test_소계가_같은_묶음부터_섹션에_짝짓는다():
    items = [{"code": "101", "description": "가", "amount": 100, "unit_price": 100, "quantity": 1},
             {"code": "201", "description": "나", "amount": 300, "unit_price": 300, "quantity": 1}]
    sections = build_sections([{"name": "B공사", "amount": 300}, {"name": "A공사", "amount": 100}], items)
    assert [(s["name"], s["lines"][0]["desc"]) for s in sections] == [("B공사", "나"), ("A공사", "가")]


def test_검산은_소계와_단가_수량과_중복을_잡아낸다():
    doc = _doc()
    assert check_record(doc) == []
    doc["sections"][0]["lines"][0]["amount"] += 1000
    doc["sections"][1]["lines"].append(dict(doc["sections"][1]["lines"][0]))
    problems = " / ".join(check_record(doc))
    assert "품목 합" in problems and "단가" in problems and "같은 줄이 2번" in problems
