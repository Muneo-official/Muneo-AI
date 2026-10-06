"""
eval/quote_llm_benchmark.py — 가견적 비교: 정답셋의 같은 입력을 범용 LLM에 주고, 엔진과 같은 채점 코드로 채점한다.

    python -m eval.quote_llm_benchmark --check                 # 키·모델 ID 확인 (과금 없음)
    python -m eval.quote_llm_benchmark --dry-run               # 프롬프트만 출력 (과금 없음)
    python -m eval.quote_llm_benchmark                         # 개선용(dev) 세트, 모델당 1회
    python -m eval.quote_llm_benchmark --split eval --final --repeats 3   # 평가용 세트 — 마지막 비교 때 한 번만
    python -m eval.quote_llm_benchmark --split holdout --final --repeats 3   # 검증용 세트
    python -m eval.quote_llm_benchmark --variant narrow        # 범위를 엔진만큼 좁게 부르게 한 조건

비교 조건
  - 입력: 엔진이 받는 값과 같다(EstimateRequest를 거친 JSON). 견적서 원문이나 정답 금액은 주지 않는다
  - 프롬프트: 모든 모델에 같은 글자. 공사비만(이윤·보험료·부가세 제외), 공종 정의, 출력 형식을 적는다
  - 웹 검색·도구 없음, 각 API의 기본 설정(추론 강도·온도)을 그대로 쓴다 — 일반 사용자가 쓰는 상태에 가깝게
  - 채점: eval/quote_benchmark.py의 score_record()·summarize()를 그대로 쓴다. 채점 코드가 둘로 갈리면 비교가 아니다
  - 모델마다 부르는 범위의 폭이 달라서 범위 적중률만으로는 비교가 안 된다. 중간값 ±13.5% 안에 정답이 든 비율
    (같은 폭 적중률)을 함께 본다
  - 같은 입력이어도 답이 실행마다 달라서 건당 여러 번 부르고, 회차마다 따로 채점해 평균과 최소~최대를 낸다
  - 답을 읽을 수 없거나(JSON 아님, 총액 없음) 거절한 건은 실패 — 적중률에서 '벗어남'으로 센다
  - 호출 자체가 실패한 건(한도 초과, 네트워크)이 있으면 그 모델은 채점하지 않는다 — 모델의 오답이 아니다

개선용 세트의 점수를 보고 프롬프트를 고치지 않는다. 고치는 것은 형식을 못 지켜 답을 읽을 수 없을 때뿐이다.

응답은 커밋하지 않는 위치(estimate_data/_gt_review/llm_runs/)에 호출마다 저장한다. 같은 프롬프트·모델·회차의
응답이 이미 있으면 다시 부르지 않는다 — 중간에 끊겨도 받은 응답에 다시 과금되지 않는다.
"""

import argparse
import concurrent.futures
import datetime
import hashlib
import json
import os
import re
import statistics
import sys

from dotenv import load_dotenv

from app.schemas.estimate import EstimateRequest
from eval.quote_benchmark import SPLITS, TARGET_HIT_RATE, load_records, require_final, score_record, summarize
from eval.quote_ground_truth import REVIEW_DIR

load_dotenv()

LLM_RUNS_DIR = REVIEW_DIR / "llm_runs"
MAX_OUTPUT_TOKENS = 16000  # 추론 토큰이 여기에 포함되는 모델이 있다. 모자라면 답이 잘려 실패로 잡힌다
WORKERS = 4

# 비교 모델 — 회사마다 일반 사용자가 쓰는 주력 모델 하나. 가격은 100만 토큰당 달러(입력, 출력), 2026-10-05 가격표.
# 비용은 기록용 추정이다. 모델 ID와 가격이 바뀌면 여기만 고친다(--check로 ID를 확인한다)
MODELS: dict[str, dict] = {
    "gpt": {"provider": "openai", "model": "gpt-6.1-sol", "price": (2.00, 10.00), "key": "OPENAI_API_KEY"},
    "gemini": {"provider": "gemini", "model": "gemini-3.8-flash", "price": (0.75, 3.75), "key": "GEMINI_API_KEY"},
    "claude": {"provider": "anthropic", "model": "claude-sonnet-5-5", "price": (2.00, 10.00), "key": "ANTHROPIC_API_KEY"},
}

SYSTEM_PROMPT = """당신은 한국의 주거 인테리어 공사 견적 전문가입니다. 주어진 공사 조건으로 실제 인테리어 업체가 낼 견적 금액을 추정합니다.

금액 기준
- 공사비만 추정합니다. 자재비와 인건비를 합한 금액입니다.
- 업체 이윤, 공과잡비(공사비의 n%로 붙는 금액), 산재·고용 보험료, 부가세는 넣지 않습니다.
- 단위는 원입니다. 만 원 단위로 쓰지 않습니다.
- 요청에 있는 공종만 추정합니다. 요청에 없는 공종(확장, 에어컨 등)은 넣지 않습니다.

공종 정의
- 창호: 샷시(발코니창·이중창 등 창문) 교체. 문은 넣지 않습니다.
- 도어: 방문, 중문, 현관문과 문틀.
- 욕실: 욕실 공사 전체(도기, 수전, 방수)에 더해, 견적서의 타일공사와 설비공사를 모두 넣습니다. 현관·주방 벽·발코니 타일과 배관 공사도 여기에 넣습니다.
- 가구: 싱크대를 포함한 주방 가구, 붙박이장, 신발장 등 제작 가구.
- 전기/조명: 배선, 스위치·콘센트, 조명 기구.
- 도장: 페인트, 탄성코트.
- 필름: 인테리어 필름(시트) 시공.
- 장판, 마루: 바닥공사 전체. 바닥재 종류에 따라 둘 중 하나로 요청됩니다.
- 도배: 벽지 시공.
- 목공: 몰딩, 걸레받이, 천장, 가벽 등. 문은 넣지 않습니다.
- 철거: 기존 마감재·설비 철거. 철거 폐기물 처리를 포함할 수 있습니다.
- 마감/공과잡비: 보양, 입주 청소, 승강기 사용료, 철물·잡자재처럼 금액이 따로 적히는 항목. 이윤이나 보험료가 아닙니다.

입력 필드
- 시공범위 "전체"는 집 전체 리모델링, "부분"은 일부 공종만 하는 공사입니다.
- 지역 "수도권"은 경기·인천입니다.
- 평수는 공급면적 기준 평형입니다.

출력 형식
아래 모양의 JSON 하나만 출력합니다. 설명, 머리말, 코드 블록 표시를 붙이지 않습니다.
{"총_견적_범위": {"최소": 정수, "중간": 정수, "최대": 정수}, "공종별_단가_범위": {"<공종>": {"최소": 정수, "중간": 정수, "최대": 정수}}}
- "공종별_단가_범위"에는 요청의 공종을 이름 그대로 모두 넣습니다.
- "중간"은 가장 가능성이 높은 금액이고, "최소"~"최대"는 실제 견적이 들어올 것으로 보는 범위입니다.
- "총_견적_범위"는 요청한 공종 전체의 공사비입니다."""

# 프롬프트 조건. "base"가 본 비교의 프롬프트이고, 나머지는 거기에 지시를 덧붙인 것이다.
# narrow: LLM은 넓게 부른다(총액 폭 48~70%, 엔진 27%). 엔진과 같은 폭으로 부르게 해도 정확도가 유지되는지 본다.
# 총액은 엔진이 참고 사례를 12건 이상 모았을 때의 폭, 공종은 엔진의 상한(최대가 최소의 1.8배)과 같다
PROMPT_VARIANTS: dict[str, str] = {
    "base": "",
    "narrow": """

범위의 폭
- "총_견적_범위"는 좁게 부릅니다. (최대 − 최소) ÷ 중간이 0.27을 넘지 않게 합니다.
- "공종별_단가_범위"는 최대가 최소의 1.8배를 넘지 않게 합니다.""",
}


def system_prompt(variant: str = "base") -> str:
    return SYSTEM_PROMPT + PROMPT_VARIANTS[variant]


USER_PROMPT = "아래 조건의 인테리어 공사 견적을 추정해 주세요.\n\n{input_json}"


def build_user_prompt(record: dict) -> str:
    """엔진에 들어가는 값과 같은 입력(스키마의 기본값·정규화를 거친 값)을 JSON으로 준다."""
    inp = EstimateRequest(**record["input"]).model_dump(exclude_none=True)
    return USER_PROMPT.format(input_json=json.dumps(inp, ensure_ascii=False, indent=2))


def prompt_hash(user_prompt: str, variant: str = "base") -> str:
    # 출력 토큰 한도도 넣는다 — 한도가 모자라 잘린 응답이 한도를 올린 뒤에도 다시 쓰이지 않게
    return hashlib.sha256(f"{system_prompt(variant)}\n{user_prompt}\n{MAX_OUTPUT_TOKENS}".encode()).hexdigest()[:16]


# ── 응답 읽기 (순수 계산) ─────────────────────────────────────────────────


def _range(value) -> dict | None:
    """{"최소","중간","최대"}가 양의 정수이고 순서가 맞으면 돌려준다. 아니면 None."""
    if not isinstance(value, dict):
        return None
    try:
        lo, mid, hi = (int(value[k]) for k in ("최소", "중간", "최대"))
    except (KeyError, TypeError, ValueError, OverflowError):  # OverflowError: 1e999처럼 무한대로 읽히는 값
        return None
    if not 0 < lo <= mid <= hi:
        return None
    return {"최소": lo, "중간": mid, "최대": hi}


def parse_response(text: str | None) -> dict:
    """LLM의 답을 엔진 출력과 같은 모양으로 바꾼다. 읽을 수 없으면 {"error": 사유}.

    코드 블록 표시나 앞뒤 설명, 다른 중괄호가 섞여 있어도 총액 범위가 든 첫 JSON 객체를 읽는다 — 형식 실수로
    금액 추정을 깎지 않는다.
    공종 범위 하나가 잘못됐으면 그 공종만 미산출(None)로 두고, 총액 범위가 잘못됐으면 실패다.
    """
    if not text or not text.strip():
        return {"error": "빈 응답"}
    decoder, data = json.JSONDecoder(), None
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "총_견적_범위" in candidate:
            data = candidate
            break
    if data is None:
        return {"error": "총액 범위가 든 JSON 없음"}
    total = _range(data.get("총_견적_범위"))
    if total is None:
        return {"error": "총액 범위가 잘못됨"}
    trades = data.get("공종별_단가_범위")
    trades = trades if isinstance(trades, dict) else {}
    ranges = {g: r for g, v in trades.items() if (r := _range(v)) is not None}
    return {"총_견적_범위": total, "공종별_단가_범위": ranges, "참고_사례_수": 0}


def call_cost(usage: dict, price: tuple[float, float]) -> float:
    return (usage.get("input", 0) * price[0] + usage.get("output", 0) * price[1]) / 1_000_000


# ── 회사별 호출 ───────────────────────────────────────────────────────────
# 반환: {"text": 답(없으면 None), "usage": {"input", "output"}, "stop": 종료 사유}. output에는 추론 토큰을 포함한다(과금 기준)


def _call_openai(model: str, user_prompt: str, system: str) -> dict:
    from openai import OpenAI

    resp = OpenAI().responses.create(
        model=model, instructions=system, input=user_prompt, max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    return {"text": resp.output_text, "stop": resp.status,
            "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}}


def _call_gemini(model: str, user_prompt: str, system: str) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=model, contents=user_prompt,
        config=types.GenerateContentConfig(system_instruction=system, max_output_tokens=MAX_OUTPUT_TOKENS),
    )
    meta = resp.usage_metadata
    stop = resp.candidates[0].finish_reason if resp.candidates else None
    return {"text": resp.text, "stop": str(stop),
            "usage": {"input": meta.prompt_token_count or 0,
                      "output": (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)}}


def _call_anthropic(model: str, user_prompt: str, system: str) -> dict:
    from pipeline.vision_client import get_client  # 서비스와 같은 클라이언트 — workspace 헤더를 같은 방식으로 붙인다

    # 거절 시 다른 모델로 넘기는 fallbacks는 쓰지 않는다 — 다른 모델의 답이 이 모델의 점수에 섞인다. 거절은 실패로 센다
    resp = get_client().messages.create(
        model=model, max_tokens=MAX_OUTPUT_TOKENS, system=system,
        messages=[{"role": "user", "content": user_prompt}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return {"text": text or None, "stop": resp.stop_reason,
            "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}}


_CALLERS = {"openai": _call_openai, "gemini": _call_gemini, "anthropic": _call_anthropic}


def fetch(name: str, record: dict, rep: int, split: str, variant: str = "base") -> dict:
    """모델 하나·정답 레코드 하나·회차 하나의 응답. 저장된 것이 있으면 그것을 쓰고, 없으면 부르고 저장한다."""
    spec = MODELS[name]
    user_prompt = build_user_prompt(record)
    digest = prompt_hash(user_prompt, variant)
    # 조건마다 폴더를 나눈다 — 같은 자리에 두면 다른 조건의 응답을 덮어쓴다
    folder = LLM_RUNS_DIR / split if variant == "base" else LLM_RUNS_DIR / split / f"variant_{variant}"
    path = folder / spec["model"] / f"{record['id']}_r{rep}.json"
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # 쓰다가 끊긴 파일 — 없는 것으로 보고 다시 부른다
            saved = {}
        if saved.get("prompt_hash") == digest:
            return {**saved, "cached": True}
    try:
        result = _CALLERS[spec["provider"]](spec["model"], user_prompt, system_prompt(variant))
    except Exception as exc:  # 회사마다 예외 종류가 다르다. 한 건의 실패로 전체를 멈추지 않고, 저장하지 않아 다음 실행에서 다시 부른다
        return {"id": record["id"], "rep": rep, "model": spec["model"], "call_error": f"{type(exc).__name__}: {exc}"}
    saved = {
        "id": record["id"], "rep": rep, "model": spec["model"], "variant": variant, "prompt_hash": digest,
        "called_at": datetime.datetime.now().isoformat(timespec="seconds"),
        **result, "cost_usd": call_cost(result["usage"], spec["price"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")  # 다 쓴 뒤에 이름을 바꾼다 — 쓰는 중에 끊겨도 반쯤 쓰인 응답 파일이 남지 않게
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return {**saved, "cached": False}


# ── 집계 ──────────────────────────────────────────────────────────────────

_KEYS = ("절대오차율_중앙값", "적중률", "같은폭_적중률", "폭_중앙값")


def aggregate_reps(summaries: list[dict]) -> dict:
    """회차별 요약(summarize_scores의 결과)을 평균과 최소~최대로 묶는다. 값이 없는 회차는 뺀다."""
    out = {"회차": len(summaries)}
    for key in _KEYS:
        values = [s[key] for s in summaries if s.get(key) is not None]
        out[key] = {"평균": statistics.mean(values), "최소": min(values), "최대": max(values)} if values else None
    for key in ("과대", "과소", "실패", "미산출", "건수"):
        out[key] = [s.get(key, 0) for s in summaries]
    return out


def _fmt(stat: dict | None) -> str:
    if stat is None:
        return "-"
    spread = f" ({stat['최소']:.0%}~{stat['최대']:.0%})" if stat["최소"] != stat["최대"] else ""
    return f"{stat['평균']:.0%}{spread}"


def print_model_report(name: str, per_rep: list[dict], calls: list[dict]) -> None:
    spec = MODELS[name]
    ok = [c for c in calls if "call_error" not in c]
    cost = sum(c["cost_usd"] for c in ok)
    new_cost = sum(c["cost_usd"] for c in ok if not c["cached"])
    tokens_in = statistics.mean(c["usage"]["input"] for c in ok) if ok else 0
    tokens_out = statistics.mean(c["usage"]["output"] for c in ok) if ok else 0
    print(f"\n=== {name} ({spec['model']}) — 호출 {len(ok)}건, 호출 오류 {len(calls) - len(ok)}건, "
          f"평균 토큰 입력 {tokens_in:.0f}/출력 {tokens_out:.0f}, 비용 ${cost:.2f} (이번 실행 ${new_cost:.2f})")
    for key, title in (("직접비", "총액 — 공사비 기준 (주 지표)"), ("총액", "총액 — 간접비 포함 기준 (참고)")):
        print(f"[{title}]")
        for group in ("전체", "전체 시공", "부분 시공", "플래그 없는 건"):
            agg = aggregate_reps([s[key][group] for s in per_rep])
            print(f"  {group:<10} 절대오차율 {_fmt(agg['절대오차율_중앙값']):<16} 같은 폭 적중률 {_fmt(agg['같은폭_적중률']):<16} "
                  f"적중률 {_fmt(agg['적중률']):<16} 폭 {_fmt(agg['폭_중앙값']):<16} "
                  f"과대 {agg['과대']}·과소 {agg['과소']} 실패 {agg['실패']}")
    print("[공종별]")
    for g in per_rep[0]["공종별"]:
        agg = aggregate_reps([s["공종별"][g] for s in per_rep if g in s["공종별"]])
        print(f"  {g:<10} 절대오차율 {_fmt(agg['절대오차율_중앙값']):<16} 같은 폭 적중률 {_fmt(agg['같은폭_적중률']):<16} "
              f"적중률 {_fmt(agg['적중률']):<16} 폭 {_fmt(agg['폭_중앙값']):<16} 과대 {agg['과대']}·과소 {agg['과소']} "
              f"건수 {agg['건수']} 미산출 {agg['미산출']} 실패 {agg['실패']}")


# ── 실행 ──────────────────────────────────────────────────────────────────


def check_models(names: list[str]) -> None:
    """키가 있는지, 모델 ID가 각 API의 모델 목록에 있는지 확인한다. 목록 조회는 과금되지 않는다."""
    for name in names:
        spec = MODELS[name]
        if not os.environ.get(spec["key"]):
            print(f"{name}: {spec['key']} 없음")
            continue
        try:
            if spec["provider"] == "openai":
                from openai import OpenAI
                ids = [m.id for m in OpenAI().models.list()]
            elif spec["provider"] == "gemini":
                from google import genai
                client = genai.Client(api_key=os.environ[spec["key"]])  # 변수로 잡아 둔다 — 목록을 읽는 중에 닫히지 않게
                ids = [m.name.removeprefix("models/") for m in client.models.list()]
            else:
                from pipeline.vision_client import get_client
                ids = [m.id for m in get_client().models.list()]
        except Exception as exc:
            print(f"{name}: 모델 목록 조회 실패 — {type(exc).__name__}: {exc}")
            continue
        found = "있음" if spec["model"] in ids else "없음"
        print(f"{name}: {spec['model']} {found}. 목록: {sorted(ids)}")


def run(split: str, names: list[str], repeats: int, variant: str = "base") -> None:
    records = load_records(split)
    missing = [MODELS[n]["key"] for n in names if not os.environ.get(MODELS[n]["key"])]
    if missing:
        raise SystemExit(f"키가 없습니다: {missing} — .env에 넣거나 --models로 모델을 고르세요")

    report = {"split": split, "repeats": repeats, "variant": variant,
              "system_prompt_hash": prompt_hash("", variant), "models": {}}
    for name in names:
        jobs = [(record, rep) for record in records for rep in range(1, repeats + 1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            calls = list(pool.map(lambda job: fetch(name, job[0], job[1], split, variant), jobs))
        errors = [f"{c['id']} r{c['rep']}: {c['call_error']}" for c in calls if "call_error" in c]
        if errors:
            # 호출 오류(한도 초과, 네트워크, 잘못된 모델 ID)는 모델의 오답이 아니다. 채점하면 적중률이 깎여 보이므로
            # 이 모델은 채점하지 않는다. 받은 응답은 저장돼 있어서 다시 실행하면 실패한 건만 다시 부른다
            print(f"\n=== {name} ({MODELS[name]['model']}) — 호출 오류 {len(errors)}건, 채점하지 않음. 다시 실행하세요",
                  *errors, sep="\n    ")
            report["models"][name] = {"model": MODELS[name]["model"], "call_errors": errors}
            continue
        by_key = {(c["id"], c["rep"]): c for c in calls}

        per_rep, rows_by_rep = [], {}
        for rep in range(1, repeats + 1):
            rows = []
            for record in records:
                rows.append(score_record(record, parse_response(by_key[(record["id"], rep)]["text"])))
            rows_by_rep[rep] = rows
            per_rep.append(summarize(rows))
        print_model_report(name, per_rep, calls)
        report["models"][name] = {
            "model": MODELS[name]["model"], "summary_per_rep": per_rep, "rows_per_rep": rows_by_rep,
            "cost_usd": sum(c["cost_usd"] for c in calls), "call_errors": [],
        }

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LLM_RUNS_DIR / (f"{split}_{stamp}.json" if variant == "base" else f"{split}_{variant}_{stamp}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**report, "run_at": stamp}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n범위 적중률 목표 {TARGET_HIT_RATE:.0%}. [OK] {len(records)}건 × {repeats}회 × {len(names)}모델 → {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=SPLITS, default="dev")
    parser.add_argument("--final", action="store_true", help="평가용(eval)·검증용(holdout) 세트 실행 확인 — 마지막 비교 때 한 번만")
    parser.add_argument("--models", nargs="+", choices=list(MODELS), default=list(MODELS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--variant", choices=list(PROMPT_VARIANTS), default="base", help="프롬프트 조건")
    parser.add_argument("--check", action="store_true", help="키와 모델 ID만 확인 (과금 없음)")
    parser.add_argument("--dry-run", action="store_true", help="첫 레코드의 프롬프트만 출력 (과금 없음)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.check:
        check_models(args.models)
        return
    require_final(parser, args)
    if args.repeats < 1:
        parser.error("--repeats는 1 이상")
    if args.dry_run:
        print(system_prompt(args.variant), "\n\n---\n", build_user_prompt(load_records(args.split)[0]), sep="")
        return
    run(args.split, args.models, args.repeats, args.variant)


if __name__ == "__main__":
    main()
