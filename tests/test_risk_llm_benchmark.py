"""
리스크 진단 LLM 비교(eval/risk_llm_benchmark.py)의 순수 계산 단위테스트 — 외부 호출은 하지 않는다.

모델의 답을 Muneo의 지적과 같은 모양으로 바꿔 같은 채점 코드에 넣는다. 여기서 틀리면 모델의 실력이 아니라
변환의 실수가 점수가 된다.
"""

import json

from eval.risk_benchmark import COMMON
from eval.risk_llm_benchmark import (
    aggregate_reps,
    build_user_prompt,
    canonical_trade,
    cut_positions,
    parse_response,
    prompt_hash,
    score_rep,
)

# ── 이미지 자르기 ─────────────────────────────────────────────────────────


def _rows(height: int, lines: list[int]) -> list[float]:
    return [0.9 if y in lines else 0.05 for y in range(height)]


def test_목표_높이_근처의_가로줄에서_자른다():
    # 26픽셀마다 표의 줄 경계가 있다. 1,100 근처의 경계(1,092)에서 자르고, 줄은 위 조각에 들어간다
    lines = list(range(0, 2000, 26))
    assert cut_positions(_rows(2000, lines), 2000) == [1093]


def test_짧은_이미지는_자르지_않는다():
    assert cut_positions(_rows(1100, []), 1100) == []
    assert cut_positions(_rows(1130, []), 1130) == []  # 조금 넘는 정도면 한 장으로 둔다


def test_긴_이미지는_여러_번_자르고_조각이_겹치지_않는다():
    lines = list(range(0, 4130, 26))
    cuts = cut_positions(_rows(4130, lines), 4130)
    assert len(cuts) == 3 and cuts == sorted(set(cuts))
    assert all((c - 1) % 26 == 0 for c in cuts)  # 모두 줄 경계


def test_가로줄이_없으면_목표_높이에서_자른다():
    assert cut_positions([0.0] * 2400, 2400) == [1101, 2202]


# ── 공종 이름 ─────────────────────────────────────────────────────────────


def test_견적서의_구분_이름을_채점하는_공종_이름으로_바꾼다():
    assert canonical_trade("도배공사") == "도배"
    assert canonical_trade("목공,도어") == "목공"   # 앞에 나온 이름
    assert canonical_trade("도기,수전") == "도기"
    assert canonical_trade(" 시트공사 ") == "시트"


def test_공종이_없는_지적은_공통이다():
    assert canonical_trade("공통") == COMMON
    assert canonical_trade("") == COMMON
    assert canonical_trade("해당 없음") == COMMON


def test_모르는_공종_이름은_그대로_둔다():
    # 어느 결함도 가리키지 못한다 — 억지로 아는 공종에 붙이지 않는다
    assert canonical_trade("커튼공사") == "커튼공사"


# ── 응답 읽기 ─────────────────────────────────────────────────────────────


def _answer(*items: dict) -> str:
    return json.dumps({"지적": list(items)}, ensure_ascii=False)


def test_지적을_Muneo의_지적과_같은_모양으로_바꾼다():
    out = parse_response(_answer({"공종": "목공,도어", "종류": "가격", "방향": "과다", "설명": "목공 인건비가 높다"}))
    assert out == {"findings": [{"trade": "목공", "kind": "가격", "direction": "과다", "text": "목공 인건비가 높다"}], "dropped": 0}


def test_방향은_가격_지적에만_두고_수량은_과다로_맞춘다():
    out = parse_response(_answer(
        {"공종": "도배공사", "종류": "수량", "방향": "없음", "설명": "부자재 660㎡"},
        {"공종": "철거공사", "종류": "누락", "방향": "과다", "설명": "폐기물 처리 없음"},
        {"공종": "바닥공사", "종류": "가격", "방향": "없음", "설명": "시세와 다름"},
    ))
    assert [f["direction"] for f in out["findings"]] == ["과다", None, None]


def test_코드_블록과_앞뒤_설명이_있어도_읽는다():
    text = "검토 결과입니다.\n```json\n" + _answer({"공종": "공통", "종류": "누락", "방향": "없음", "설명": "양중비 없음"}) + "\n```\n이상입니다."
    assert parse_response(text)["findings"][0]["trade"] == COMMON


def test_종류가_여섯_갈래에_없는_지적만_버린다():
    out = parse_response(_answer({"공종": "도배공사", "종류": "기타", "설명": "글씨가 작다"},
                                 {"공종": "도배공사", "종류": "중복", "설명": "식대가 두 번"}))
    assert [f["kind"] for f in out["findings"]] == ["중복"] and out["dropped"] == 1


def test_문제가_없다는_답은_빈_목록이고_실패가_아니다():
    assert parse_response('{"지적": []}') == {"findings": [], "dropped": 0}


def test_읽을_수_없는_답은_사유를_돌려준다():
    assert "error" in parse_response(None)
    assert "error" in parse_response("견적서를 검토할 수 없습니다.")
    assert "error" in parse_response('{"지적": "없음"}')


# ── 프롬프트와 저장 열쇠 ──────────────────────────────────────────────────


def test_공사_정보에_결함_판의_조건이_들어간다():
    info = {"공간유형": "아파트", "평수": 25, "지역": "서울", "층수": 9, "엘리베이터": False}
    prompt = build_user_prompt(info)
    assert "9층" in prompt and "엘리베이터: 없음" in prompt and "25평" in prompt


def test_이미지가_다르면_저장_열쇠가_다르다():
    assert prompt_hash("같은 글", [b"clean"]) != prompt_hash("같은 글", [b"planted"])
    assert prompt_hash("같은 글", [b"a", b"b"]) == prompt_hash("같은 글", [b"a", b"b"])


# ── 채점 연결 ─────────────────────────────────────────────────────────────

RECORD = {"id": "rk-900", "planted": [
    {"type": "P1", "kind": "가격", "section": "목공,도어", "direction": "과다"},
    {"type": "Q1", "kind": "수량", "section": "도배공사", "direction": "과다"},
    {"type": "M2", "kind": "누락", "section": None},
]}


def _findings(*items: tuple) -> dict:
    return {"findings": [{"trade": t, "kind": k, "direction": d, "text": text} for t, k, d, text in items], "dropped": 0}


def test_깨끗한_판에도_있던_지적은_찾은_것이_아니다():
    always = ("기타", "불분명", None, "별도 추가")
    answers = {
        ("rk-900", "clean"): _findings(always),
        ("rk-900", "planted"): _findings(always, ("목공", "가격", "과다", "인건비 64만"), ("도배", "수량", "과다", "660㎡"),
                                         (COMMON, "누락", None, "9층인데 양중비가 없다")),
    }
    rows, summary = score_rep([RECORD], answers)
    assert [d["found"] for d in rows[0]["defects"]] == [True, True, True]
    assert (summary["새_지적"], summary["새_지적_적중"], summary["깨끗한_판_지적_수_평균"], summary["실패한_판"]) == (3, 3, 1.0, 0)


def test_가격의_방향이_반대면_찾은_것이_아니다():
    answers = {("rk-900", "clean"): _findings(), ("rk-900", "planted"): _findings(("목공", "가격", "과소", "너무 싸다"))}
    rows, _ = score_rep([RECORD], answers)
    assert rows[0]["defects"][0]["found"] is False and rows[0]["new_count"] == 1


def test_읽을_수_없는_판은_지적이_없는_것으로_채점하고_따로_센다():
    answers = {("rk-900", "clean"): _findings(), ("rk-900", "planted"): {"error": "빈 응답"}}
    rows, summary = score_rep([RECORD], answers)
    assert summary["찾은_결함"] == 0 and summary["실패한_판"] == 1 and rows[0]["errors"] == ["빈 응답"]


def test_회차별_요약을_평균과_최소_최대로_묶는다():
    reps = [{"찾은_비율": 0.5, "새_지적_적중률": 1.0, "깨끗한_판_지적_수_평균": 3.0, "찾은_결함": 9, "심은_결함": 18},
            {"찾은_비율": 0.7, "새_지적_적중률": None, "깨끗한_판_지적_수_평균": 5.0, "찾은_결함": 12, "심은_결함": 18}]
    agg = aggregate_reps(reps)
    assert agg["찾은_비율"] == {"평균": 0.6, "최소": 0.5, "최대": 0.7}
    assert agg["새_지적_적중률"] == {"평균": 1.0, "최소": 1.0, "최대": 1.0}  # 값이 없는 회차는 뺀다
    assert agg["찾은_결함"] == [9, 12]
