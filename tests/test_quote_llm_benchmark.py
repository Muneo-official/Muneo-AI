"""
가견적 LLM 비교(eval/quote_llm_benchmark.py)의 단위테스트 — 응답 읽기, 실패 처리, 회차 집계, 저장된 응답 재사용.
API는 부르지 않는다. 금액은 지어낸 값이다.
"""

import json

import pytest

from eval import quote_llm_benchmark as llm
from eval.quote_benchmark import score_record, summarize, summarize_scores


def _record(**truth) -> dict:
    return {
        "id": "gt-t01", "flags": [],
        "source": {"article_id": "1", "request_url": "u"},
        "input": {"공종": ["도배", "장판"], "시공범위": "부분", "평수": 30, "지역": "서울"},
        "truth": {"비교_총액": 5_500_000, "비교_직접비": 5_000_000,
                  "공종별": {"도배": 3_000_000, "장판": 2_000_000}, **truth},
    }


def _answer(total=(4_000_000, 5_000_000, 6_000_000), **trades) -> str:
    def rng(v):
        return {"최소": v[0], "중간": v[1], "최대": v[2]}
    return json.dumps({"총_견적_범위": rng(total), "공종별_단가_범위": {g: rng(v) for g, v in trades.items()}},
                      ensure_ascii=False)


# ── 응답 읽기 ──────────────────────────────────────────────────────────────


def test_답을_엔진_출력과_같은_모양으로_읽는다():
    out = llm.parse_response(_answer(도배=(2_500_000, 3_000_000, 3_500_000)))

    assert out["총_견적_범위"] == {"최소": 4_000_000, "중간": 5_000_000, "최대": 6_000_000}
    assert out["공종별_단가_범위"] == {"도배": {"최소": 2_500_000, "중간": 3_000_000, "최대": 3_500_000}}


def test_코드_블록_표시나_설명이_붙어도_읽는다():
    text = "견적은 다음과 같습니다.\n```json\n" + _answer() + "\n```\n참고용 추정입니다."

    assert llm.parse_response(text)["총_견적_범위"]["중간"] == 5_000_000


@pytest.mark.parametrize("text", [None, "", "죄송하지만 추정할 수 없습니다.", "{최소: 1,", '{"공종별_단가_범위": {}}'])
def test_읽을_수_없는_답은_실패다(text):
    assert "error" in llm.parse_response(text)


@pytest.mark.parametrize("total", [(6_000_000, 5_000_000, 4_000_000), (0, 5_000_000, 6_000_000), ("a", "b", "c")])
def test_총액_범위가_뒤집혔거나_양수가_아니면_실패다(total):
    assert "error" in llm.parse_response(_answer(total=total))


def test_공종_범위_하나가_잘못됐으면_그_공종만_미산출이다():
    out = llm.parse_response(_answer(도배=(3_500_000, 3_000_000, 2_500_000), 장판=(1_500_000, 2_000_000, 2_500_000)))

    assert list(out["공종별_단가_범위"]) == ["장판"]
    row = score_record(_record(), out)
    assert row["공종별"]["도배"] is None and row["공종별"]["장판"]["적중"]


# ── 채점 연결 ──────────────────────────────────────────────────────────────


def test_엔진과_같은_채점_코드로_채점된다():
    row = score_record(_record(), llm.parse_response(_answer()))

    assert row["직접비"] == {"오차율": 0.0, "적중": True, "같은폭_적중": True, "폭": 0.4}
    assert row["총액"]["적중"]  # 5,500,000은 4,000,000~6,000,000 안


def test_읽을_수_없는_답은_적중률에서_벗어남으로_센다():
    rows = [score_record(_record(), llm.parse_response(_answer())),
            score_record(_record(), llm.parse_response("추정할 수 없습니다"))]
    s = summarize(rows)["직접비"]["전체"]

    assert s["건수"] == 1 and s["실패"] == 1 and s["적중률"] == 0.5 and s["같은폭_적중률"] == 0.5


def test_넓게_부른_범위로_맞춘_건은_같은_폭_적중이_아니다():
    # 정답 5,000,000 / 중간 4,000,000 (−20%). 부른 범위(3,000,000~6,000,000)에는 들지만 ±13.5%에는 안 든다
    row = score_record(_record(), llm.parse_response(_answer(total=(3_000_000, 4_000_000, 6_000_000))))

    assert row["직접비"]["적중"] and not row["직접비"]["같은폭_적중"]


# ── 회차 집계 ──────────────────────────────────────────────────────────────


def test_회차별_요약을_평균과_최소_최대로_묶는다():
    agg = llm.aggregate_reps([
        {"건수": 10, "절대오차율_중앙값": 0.2, "적중률": 0.3, "폭_중앙값": 0.5, "과대": 6, "과소": 4, "실패": 0},
        {"건수": 10, "절대오차율_중앙값": 0.4, "적중률": 0.5, "폭_중앙값": 0.5, "과대": 7, "과소": 3, "실패": 0},
    ])

    assert agg["회차"] == 2
    assert agg["절대오차율_중앙값"] == pytest.approx({"평균": 0.3, "최소": 0.2, "최대": 0.4})
    assert agg["적중률"] == pytest.approx({"평균": 0.4, "최소": 0.3, "최대": 0.5})
    assert agg["과대"] == [6, 7]


def test_전부_실패한_회차는_적중률_0으로_평균에_들어간다():
    # 값을 비워 두면 그 회차가 평균에서 빠져, 두 번 중 한 번을 통째로 실패한 모델이 100%로 보인다
    ok = score_record(_record(), llm.parse_response(_answer()))
    agg = llm.aggregate_reps([summarize_scores([], failed=2), summarize_scores([ok["직접비"]] * 2)])

    assert agg["적중률"]["평균"] == 0.5 and agg["같은폭_적중률"]["평균"] == 0.5
    assert agg["절대오차율_중앙값"]["평균"] == 0.0  # 오차율은 구할 수 없는 회차를 뺀다
    assert agg["실패"] == [2, 0]


def test_다른_중괄호가_섞여_있어도_총액이_든_JSON을_읽는다():
    text = '형식 예시 {"a": 1} 답: ' + _answer() + " (범위는 {최소}~{최대})"

    assert llm.parse_response(text)["총_견적_범위"]["중간"] == 5_000_000


def test_무한대로_읽히는_금액은_멈추지_않고_실패로_처리한다():
    assert "error" in llm.parse_response('{"총_견적_범위": {"최소": 1, "중간": 2, "최대": 1e999}}')


# ── 프롬프트·비용 ──────────────────────────────────────────────────────────


def test_프롬프트에는_입력만_들어가고_정답은_들어가지_않는다():
    prompt = llm.build_user_prompt(_record())

    assert '"평수": 30' in prompt and '"자재등급": "중급"' in prompt  # 스키마 기본값이 채워진, 엔진이 받는 값
    assert "5000000" not in prompt and "비교_직접비" not in prompt and "gt-t01" not in prompt


def test_비용은_입력과_출력_토큰에_가격을_곱한다():
    assert llm.call_cost({"input": 1_000_000, "output": 500_000}, (2.0, 10.0)) == 7.0


# ── 저장된 응답 재사용 ─────────────────────────────────────────────────────


@pytest.fixture
def fake_api(tmp_path, monkeypatch):
    calls = []

    def caller(model, user_prompt, system):
        calls.append(system)
        return {"text": _answer(), "stop": "end", "usage": {"input": 1000, "output": 2000}}

    monkeypatch.setattr(llm, "LLM_RUNS_DIR", tmp_path)
    monkeypatch.setitem(llm._CALLERS, "openai", caller)
    return calls


def test_받은_응답은_다시_부르지_않는다(fake_api):
    first = llm.fetch("gpt", _record(), 1, "dev")
    second = llm.fetch("gpt", _record(), 1, "dev")

    assert len(fake_api) == 1
    assert not first["cached"] and second["cached"] and second["text"] == first["text"]
    assert first["cost_usd"] == pytest.approx(llm.call_cost({"input": 1000, "output": 2000}, llm.MODELS["gpt"]["price"]))


def test_회차가_다르거나_프롬프트가_바뀌면_다시_부른다(fake_api, monkeypatch):
    llm.fetch("gpt", _record(), 1, "dev")
    llm.fetch("gpt", _record(), 2, "dev")
    assert len(fake_api) == 2

    monkeypatch.setattr(llm, "SYSTEM_PROMPT", llm.SYSTEM_PROMPT + " ")
    llm.fetch("gpt", _record(), 1, "dev")
    assert len(fake_api) == 3


def test_프롬프트_조건이_다르면_따로_부르고_따로_저장한다(fake_api, tmp_path):
    llm.fetch("gpt", _record(), 1, "dev")
    llm.fetch("gpt", _record(), 1, "dev", "narrow")
    again = llm.fetch("gpt", _record(), 1, "dev")

    assert len(fake_api) == 2 and again["cached"]  # 좁은 조건의 응답이 본 조건의 응답을 덮어쓰지 않는다
    assert "0.27" in fake_api[1] and "0.27" not in fake_api[0]
    assert fake_api[1].startswith(fake_api[0])  # 본 프롬프트에 지시만 덧붙인다
    assert len(list(tmp_path.rglob("gt-t01_r1.json"))) == 2


def test_호출이_실패하면_저장하지_않고_실패로_돌려준다(tmp_path, monkeypatch):
    def broken(model, user_prompt, system):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(llm, "LLM_RUNS_DIR", tmp_path)
    monkeypatch.setitem(llm._CALLERS, "openai", broken)
    result = llm.fetch("gpt", _record(), 1, "dev")

    assert "rate limit" in result["call_error"]
    assert not list(tmp_path.rglob("*.json"))


def test_쓰다가_끊긴_응답_파일은_없는_것으로_보고_다시_부른다(fake_api, tmp_path):
    path = tmp_path / "dev" / llm.MODELS["gpt"]["model"] / "gt-t01_r1.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"id": "gt-t01", "prom', encoding="utf-8")

    assert not llm.fetch("gpt", _record(), 1, "dev")["cached"]
    assert len(fake_api) == 1 and json.loads(path.read_text(encoding="utf-8"))["text"]


def test_출력_토큰_한도를_바꾸면_저장된_응답을_다시_쓰지_않는다(fake_api, monkeypatch):
    llm.fetch("gpt", _record(), 1, "dev")
    monkeypatch.setattr(llm, "MAX_OUTPUT_TOKENS", llm.MAX_OUTPUT_TOKENS * 2)
    llm.fetch("gpt", _record(), 1, "dev")

    assert len(fake_api) == 2


def test_호출_오류가_있는_모델은_채점하지_않는다(tmp_path, monkeypatch, capsys):
    def broken(model, user_prompt, system):
        raise RuntimeError("rate limit")

    monkeypatch.setattr(llm, "LLM_RUNS_DIR", tmp_path)
    monkeypatch.setattr(llm, "load_records", lambda split: [_record()])
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    monkeypatch.setitem(llm._CALLERS, "openai", broken)
    llm.run("dev", ["gpt"], 1)

    report = json.loads(next(tmp_path.glob("dev_*.json")).read_text(encoding="utf-8"))
    assert "summary_per_rep" not in report["models"]["gpt"] and report["models"]["gpt"]["call_errors"]
    assert "채점하지 않음" in capsys.readouterr().out


# ── 평가용 세트 잠금 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("split", ["eval", "holdout"])
def test_평가용_검증용_세트는_final_없이_실행되지_않는다(monkeypatch, split):
    monkeypatch.setattr("sys.argv", ["quote_llm_benchmark", "--split", split])
    monkeypatch.setattr(llm, "run", lambda *a: pytest.fail("실행되면 안 된다"))

    with pytest.raises(SystemExit):
        llm.main()
