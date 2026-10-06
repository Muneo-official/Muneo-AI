"""
리스크 벤치마크 채점의 단위테스트 — 결함 판에서 새로 생긴 지적만 따지고, 같은 종류·같은 공종일 때만 찾은 것으로 본다.

실제 사례: 실제 견적서 368건에 리스크 진단 규칙을 적용하면 견적서 한 장에 지적이 보통 6개 나온다. 지적 수가 많은
시스템이 "찾은 비율"만으로 유리해지지 않도록, 깨끗한 판에도 있던 지적은 찾은 것으로 치지 않는다.
"""

import pytest

from eval import risk_benchmark as rb


def _f(trade: str, kind: str, direction=None, text: str = "") -> dict:
    return {"trade": trade, "kind": kind, "direction": direction, "text": text}


def _d(defect_type: str, kind: str, section, **more) -> dict:
    return {"type": defect_type, "kind": kind, "section": section, **more}


# ── 응답에서 지적 꺼내기 ───────────────────────────────────────────────────


def _report(*sections) -> dict:
    return {"report": {"process_sections": [{"process": p, "items": items} for p, items in sections]}}


def test_정상_품목은_지적이_아니다():
    report = _report(("도배", [{"status": "정상", "title": "실크벽지", "description": "1,000,000원"},
                             {"status": "중복", "title": "동일 항목 중복 기재", "description": "인건비 항목이 2회 반복되었습니다."}]))
    assert [(f["trade"], f["kind"]) for f in rb.muneo_findings(report)] == [("도배", "중복")]


def test_가격_지적은_불분명이_아니라_가격으로_세고_방향은_제목에서_읽는다():
    high = {"status": "불분명", "title": "가구 단가가 시세보다 높음", "description": "단가를 비교한 4개 품목 중 3개가 …"}
    low = {"status": "불분명", "title": "욕실 단가가 시세보다 낮음", "description": "단가를 비교한 6개 품목 중 4개가 …"}
    got = rb.muneo_findings(_report(("가구", [high]), ("욕실", [low])))
    assert [(f["kind"], f["direction"]) for f in got] == [("가격", "과다"), ("가격", "과소")]


def test_양중_지적은_공종_없는_누락으로_센다():
    item = {"status": "불분명", "title": "고층 시공 운반/양중 비용 정보 미기재", "description": "9층 시공 조건이지만 …"}
    assert [(f["trade"], f["kind"]) for f in rb.muneo_findings(_report(("공통", [item])))] == [(rb.COMMON, "누락")]


def test_품명에_운반이_든_줄의_중복_지적은_양중_지적으로_바뀌지_않는다():
    item = {"status": "중복", "title": "동일 항목 중복 기재", "description": "철거 인건비(조공):자재운반포함 항목이 2회 반복되었습니다."}
    assert [(f["trade"], f["kind"]) for f in rb.muneo_findings(_report(("철거", [item])))] == [("철거", "중복")]


# ── 새 지적 ───────────────────────────────────────────────────────────────


def test_깨끗한_판에도_있던_지적은_새_지적이_아니다():
    clean = [_f("욕실", "누락"), _f("목공", "누락")]
    planted = [_f("욕실", "누락"), _f("목공", "누락"), _f("철거", "누락")]
    assert rb.new_findings(clean, planted) == [_f("철거", "누락")]


def test_같은_지적이_늘어나면_늘어난_만큼만_새_지적이다():
    clean = [_f("도배", "불분명")]
    planted = [_f("도배", "불분명"), _f("도배", "불분명"), _f("도배", "불분명")]
    assert len(rb.new_findings(clean, planted)) == 2


def test_가격_지적은_금액만_바뀌면_새_지적이_아니고_방향이_바뀌면_새_지적이다():
    # 깨끗한 판에서 이미 "가구가 시세보다 높다"고 했으면, 결함 판에서 금액만 커진 같은 지적은 늘 하던 지적이다
    clean = [_f("가구", "가격", "과다", text="견적 금액 5,000,000원")]
    assert rb.new_findings(clean, [_f("가구", "가격", "과다", text="견적 금액 10,000,000원")]) == []
    assert len(rb.new_findings(clean, [_f("가구", "가격", "과소", text="견적 금액 1,000,000원")])) == 1


# ── 지적이 결함을 가리키는가 ───────────────────────────────────────────────


@pytest.mark.parametrize(("section", "trade"), [
    ("타일공사", "욕실"), ("도기,수전", "설비"), ("목공,도어", "도어"), ("창호공사", "도어"), ("전기,조명", "전기/조명"),
    ("시트공사", "필름"), ("바닥공사", "바닥"), ("기타공사", "공과잡비"), ("가구공사", "주방"),
])
def test_섹션과_공종은_묶음으로_맞춘다(section, trade):
    assert rb.points_at(_f(trade, "누락"), _d("M1", "누락", section))


def test_종류가_다르면_가리킨_것이_아니다():
    assert not rb.points_at(_f("욕실", "불분명"), _d("M1", "누락", "타일공사"))
    assert not rb.points_at(_f("도배", "누락"), _d("M1", "누락", "타일공사"))


def test_가격은_방향까지_맞아야_한다():
    defect = _d("P3", "가격", "가구공사", direction="과소")
    assert rb.points_at(_f("가구", "가격", "과소"), defect)
    assert not rb.points_at(_f("가구", "가격", "과다"), defect)


def test_다른_공종_중복은_두_섹션_중_어느_쪽을_말해도_된다():
    defect = _d("D2", "중복", "기타공사", also="철거공사")
    assert rb.points_at(_f("철거", "중복"), defect) and rb.points_at(_f("공과잡비", "중복"), defect)
    assert not rb.points_at(_f("도배", "중복"), defect)


def test_조건_누락은_공종_없이_양중을_말하면_된다():
    defect = _d("M2", "누락", None)
    assert rb.points_at(_f(rb.COMMON, "누락"), defect)
    assert rb.points_at(_f("기타", "누락", text="사다리차 비용이 없습니다"), defect)
    assert not rb.points_at(_f("욕실", "누락", text="방수가 없습니다"), defect)


# ── 채점 ──────────────────────────────────────────────────────────────────


def test_깨끗한_판에도_있던_지적으로는_결함을_찾은_것이_되지_않는다():
    defects = [_d("M1", "누락", "타일공사")]
    always = [_f("욕실", "누락")]  # 결함과 무관하게 늘 나오는 지적
    assert rb.score_record(defects, clean=always, planted=always)["defects"][0]["found"] is False
    assert rb.score_record(defects, clean=[], planted=always)["defects"][0]["found"] is True


def test_지적_하나로_결함_둘을_찾은_것이_되지_않는다():
    # 도기공사와 수전공사는 같은 공종 묶음이다. "욕실이 시세보다 높다" 한 줄로 두 결함을 모두 찾은 것이 되면 안 된다
    defects = [_d("P1", "가격", "도기공사", direction="과다"), _d("P2", "가격", "수전공사", direction="과다")]
    row = rb.score_record(defects, clean=[], planted=[_f("욕실", "가격", "과다")])
    assert [d["found"] for d in row["defects"]] == [True, False] and row["new_hits"] == 1

    both = rb.score_record(defects, clean=[], planted=[_f("욕실", "가격", "과다"), _f("설비", "가격", "과다")])
    assert [d["found"] for d in both["defects"]] == [True, True]


def test_새_지적의_적중률은_심은_결함을_가리킨_새_지적의_비율이다():
    defects = [_d("D1", "중복", "도배공사"), _d("P1", "가격", "가구공사", direction="과다")]
    planted = [_f("도배", "중복"), _f("목공", "누락"), _f("철거", "불분명")]
    row = rb.score_record(defects, clean=[], planted=planted)
    assert (row["new_count"], row["new_hits"]) == (3, 1)
    assert [d["found"] for d in row["defects"]] == [True, False]

    summary = rb.summarize([{"id": "x", **row}])
    assert summary["찾은_비율"] == 0.5 and summary["새_지적_적중률"] == pytest.approx(1 / 3)
    assert summary["유형별"] == {"D1": {"심음": 1, "찾음": 1}, "P1": {"심음": 1, "찾음": 0}}


def test_평가용_세트는_final_없이_실행되지_않는다(monkeypatch):
    monkeypatch.setattr("sys.argv", ["risk_benchmark", "--split", "eval"])
    monkeypatch.setattr(rb, "run", lambda *a: pytest.fail("실행되면 안 된다"))
    with pytest.raises(SystemExit):
        rb.main()
