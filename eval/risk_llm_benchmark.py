"""
eval/risk_llm_benchmark.py — 리스크 진단 비교: 결함을 심은 같은 견적서를 범용 LLM에 주고, Muneo와 같은 채점 코드로 채점한다.

    python -m eval.risk_llm_benchmark --check                  # 키·모델 ID 확인 (과금 없음)
    python -m eval.risk_llm_benchmark --dry-run                # 프롬프트와 이미지 조각 수만 출력 (과금 없음)
    python -m eval.risk_llm_benchmark                          # 개선용(dev) 세트, 모델당 1회
    python -m eval.risk_llm_benchmark --records rk-005         # 한 건만 (시험 삼아 돌릴 때)
    python -m eval.risk_llm_benchmark --split eval --final --repeats 3   # 평가용 세트 — 마지막 비교 때 한 번만

비교 조건
  - 입력: Muneo가 받는 것과 같다 — 견적서 이미지(깨끗한 판, 결함 판을 따로)와 공사 정보
  - 이미지는 세로로 길어서(2,000~4,000픽셀) 통째로 주면 API가 줄여서 글씨가 뭉개진다. 표의 줄 경계에서 잘라 여러
    장으로 준다 — Muneo도 안에서 잘라서 읽는다. 겹치는 구간 없이 자른다(겹치면 같은 줄이 두 번 보여 중복으로 읽힌다)
  - 프롬프트: 모든 모델에 같은 글자. 리스크 여섯 갈래의 정의와 출력 형식을 준다. 결함을 심었다는 말은 하지 않는다
  - 웹 검색·도구 없음, 각 API의 기본 설정(추론 강도·온도)을 그대로 쓴다
  - 채점: eval/risk_benchmark.py의 score_record()·summarize()를 그대로 쓴다. 채점 코드가 둘로 갈리면 비교가 아니다
  - 같은 이미지여도 답이 실행마다 달라서 건당 여러 번 부르고, 회차마다 따로 채점해 평균과 최소~최대를 낸다
  - 답을 읽을 수 없는 판(JSON 아님, 거절)은 지적이 없는 것으로 채점하고 "실패"로 따로 센다
  - 호출 자체가 실패한 건(한도 초과, 네트워크)이 있으면 그 모델은 채점하지 않는다 — 모델의 오답이 아니다

개선용 세트의 점수를 보고 프롬프트를 고치지 않는다. 고치는 것은 형식을 못 지켜 답을 읽을 수 없을 때뿐이다.
고친 것 하나(2026-10-06, 평가용 실행 전): "두 공종에 걸친 문제는 그중 한 공종의 이름을 적는다". 개선용에서 모델이 두 공종에
걸친 중복을 정확히 찾고도 공종을 "공통"으로 적어 못 찾은 것으로 채점됐다. 적는 방식을 맞춘 것이고, 모델에 유리한 쪽이다.

응답은 커밋하지 않는 위치(estimate_data/_risk_gt/llm_runs/)에 호출마다 저장한다. 같은 프롬프트·이미지·모델·회차의
응답이 이미 있으면 다시 부르지 않는다 — 중간에 끊겨도 받은 응답에 다시 과금되지 않는다.
"""

import argparse
import base64
import concurrent.futures
import datetime
import hashlib
import io
import json
import os
import pathlib
import re
import statistics
import sys

from dotenv import load_dotenv

from eval.quote_benchmark import require_final
from eval.quote_llm_benchmark import MAX_OUTPUT_TOKENS, MODELS, call_cost, check_models
from eval.risk_benchmark import COMMON, SECTION_TRADES, load_records, score_record, summarize
from eval.risk_ground_truth import RISK_DIR, image_paths

load_dotenv()

LLM_RUNS_DIR = RISK_DIR / "llm_runs"
WORKERS = 4
PAGE_HEIGHT = 1100   # 이미지 한 조각의 높이(픽셀). 가로 1,100과 비슷하게 두어 API가 줄여도 글씨가 남게 한다
CUT_SEARCH = 40      # 자를 자리를 이 범위 안의 가로줄(표의 줄 경계)에서 찾는다
KINDS = ("누락", "중복", "불분명", "가격", "수량", "계산")
DIRECTIONS = {"과다": "과다", "과소": "과소"}
_COMMON_WORDS = ("공통", "전체", "해당 없음", "해당없음", "없음")

SYSTEM_PROMPT = """당신은 한국의 주거 인테리어 공사 견적서를 검토하는 전문가입니다. 소비자가 업체에서 받은 견적서를 계약하기 전에 살펴보고, 소비자가 손해를 볼 수 있는 문제(리스크)를 찾아 줍니다.

입력
- 공사 정보(공간 유형, 평수, 지역, 층수, 엘리베이터 유무 등)
- 견적서 이미지. 견적서 한 장을 위에서부터 차례로 자른 여러 장입니다. 이어 붙여서 하나의 견적서로 읽으세요

찾을 문제의 종류 (여섯 가지)
- 누락: 공사에 꼭 필요한 항목이나, 공사 정보의 조건 때문에 드는 비용이 견적서에 없다
- 중복: 같은 항목이 두 번 들어가 있다 (같은 공종 안에서, 또는 서로 다른 공종에)
- 불분명: 무엇을 얼마나 하는지 알 수 없다 (제품·규격이 없음, 금액 없이 "별도"·"협의"로만 적힘, 세부 내역 없이 큰 금액이 한 줄)
- 가격: 단가나 금액이 통상적인 시세보다 뚜렷하게 높거나 낮다
- 수량: 집의 크기에 비해 수량이 지나치게 많다
- 계산: 소계나 합계가 품목 금액의 합과 맞지 않는다

규칙
- 실제로 문제라고 판단한 것만 적습니다. 문제가 없으면 빈 목록을 냅니다
- 지적 하나는 문제 하나입니다. 같은 문제를 여러 번 적지 않습니다
- 공종은 견적서에 적힌 공종(구분) 이름을 그대로 씁니다. 특정 공종에 속하지 않는 문제는 "공통"으로 적습니다
- 두 공종에 걸친 문제(같은 항목이 서로 다른 두 공종에 들어간 경우 등)는 "공통"이 아니라 그중 한 공종의 이름을 적습니다
- 가격 지적은 방향을 "과다"(비쌈) 또는 "과소"(쌈)로 적습니다. 그 밖의 종류는 방향을 "없음"으로 적습니다

출력 형식
- 아래 JSON 하나만 출력합니다. 설명 글이나 코드 블록 표시를 붙이지 않습니다
{"지적": [{"공종": "<공종 이름 또는 공통>", "종류": "<누락|중복|불분명|가격|수량|계산>", "방향": "<과다|과소|없음>", "설명": "<어느 줄이 왜 문제인지 한두 문장>"}]}"""


def build_user_prompt(info: dict) -> str:
    """공사 정보. Muneo에 넘기는 값과 같다(eval/risk_benchmark.py의 AnalyzeRiskCommand) — 방 수와 건물 연식은 고정값이다."""
    return ("공사 정보\n"
            f"- 공간 유형: {info['공간유형']}\n- 평수: {info['평수']}평\n- 방 수: 3개\n- 지역: {info['지역']}\n"
            f"- 층수: {info['층수']}층\n- 엘리베이터: {'있음' if info['엘리베이터'] else '없음'}\n- 건물 연식: 10~20년\n\n"
            "첨부한 이미지가 견적서입니다. 이 견적서의 문제를 찾아 주세요.")


# ── 이미지 자르기 ─────────────────────────────────────────────────────────


def cut_positions(dark_rows: list[float], height: int, page_height: int = PAGE_HEIGHT, search: int = CUT_SEARCH) -> list[int]:
    """세로로 긴 이미지를 자를 y 좌표들. 목표 높이 근처에서 가장 진한 가로줄(표의 줄 경계)을 고른다.

    dark_rows: 행마다 어두운 픽셀의 비율. 글자 한가운데를 자르면 그 줄을 어느 조각에서도 읽을 수 없다.
    """
    cuts, top = [], 0
    while height - top > page_height + search:
        target = top + page_height
        lo, hi = max(top + 1, target - search), min(height - 1, target + search)
        cut = max(range(lo, hi + 1), key=lambda y: (dark_rows[y], -abs(y - target)))
        cuts.append(cut + 1)  # 가로줄은 위 조각에 넣는다
        top = cut + 1
    return cuts


def split_image(path: pathlib.Path) -> list[bytes]:
    """견적서 이미지를 읽을 수 있는 크기의 PNG 조각들로. 겹치는 구간은 없다."""
    from PIL import Image

    img = Image.open(path).convert("RGB")
    gray = img.convert("L")
    width, height = img.size
    data = gray.tobytes()
    dark_rows = [sum(1 for v in data[y * width:(y + 1) * width] if v < 100) / width for y in range(height)]
    edges = [0, *cut_positions(dark_rows, height), height]
    pages = []
    for top, bottom in zip(edges, edges[1:]):
        buf = io.BytesIO()
        img.crop((0, top, width, bottom)).save(buf, format="PNG")
        pages.append(buf.getvalue())
    return pages


def prompt_hash(user_prompt: str, pages: list[bytes]) -> str:
    # 출력 토큰 한도도 넣는다 — 한도가 모자라 잘린 응답이 한도를 올린 뒤에도 다시 쓰이지 않게
    h = hashlib.sha256(f"{SYSTEM_PROMPT}\n{user_prompt}\n{MAX_OUTPUT_TOKENS}".encode())
    for page in pages:
        h.update(hashlib.sha256(page).digest())
    return h.hexdigest()[:16]


# ── 응답 읽기 (순수 계산) ─────────────────────────────────────────────────


def canonical_trade(text: str) -> str:
    """모델이 적은 공종 이름을 채점 코드가 아는 공종 이름으로.

    모델은 견적서의 구분 이름("도기,수전", "목공사")을 그대로 적는다. 그 이름에 든 낱말 중 가장 앞에 나오는 공종
    이름을 고른다 — "목공,도어"는 "목공"이다. 아는 낱말이 없으면 적힌 대로 둔다(어느 결함도 가리키지 못한다).
    """
    name = text.strip()
    if not name or any(w in name for w in _COMMON_WORDS):
        return COMMON
    best: tuple[int, str] | None = None
    for words, trades in SECTION_TRADES:
        if not any(w in name for w in words):
            continue
        for trade in sorted(trades):
            at = name.find(trade)
            if at >= 0 and (best is None or (at, trade) < best):
                best = (at, trade)
        if best is None:  # 구분 이름의 낱말("싱크")은 있는데 공종 이름("가구")은 없다 — 그 묶음의 이름 하나로
            best = (len(name), sorted(trades)[0])
    return best[1] if best else name


def parse_response(text: str | None) -> dict:
    """LLM의 답을 지적 목록으로. 반환: {"findings": [...]} 또는 {"error": 사유}.

    지적 하나는 Muneo의 지적과 같은 모양이다 — {"trade", "kind", "direction", "text"}.
    코드 블록 표시나 앞뒤 설명이 섞여 있어도 "지적"이 든 첫 JSON 객체를 읽는다. 종류가 여섯 갈래에 없는 지적은
    버린다(채점할 수 없다) — 형식 실수 한 줄로 답 전체를 버리지는 않는다.
    """
    if not text or not text.strip():
        return {"error": "빈 응답"}
    decoder, data = json.JSONDecoder(), None
    for match in re.finditer(r"\{", text):
        try:
            candidate, _ = decoder.raw_decode(text, match.start())
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and isinstance(candidate.get("지적"), list):
            data = candidate
            break
    if data is None:
        return {"error": "지적 목록이 든 JSON 없음"}
    findings = []
    for item in data["지적"]:
        if not isinstance(item, dict) or str(item.get("종류", "")).strip() not in KINDS:
            continue
        kind = str(item["종류"]).strip()
        findings.append({
            "trade": canonical_trade(str(item.get("공종") or "")), "kind": kind,
            # 방향은 가격 지적에만 뜻이 있다. Muneo의 수량 지적은 늘 "과다"라서 같은 값으로 맞춘다 — 같은 지적인지
            # 가리는 열쇠에 방향이 들어간다
            "direction": DIRECTIONS.get(str(item.get("방향", "")).strip()) if kind == "가격" else "과다" if kind == "수량" else None,
            "text": str(item.get("설명") or ""),
        })
    return {"findings": findings, "dropped": len(data["지적"]) - len(findings)}


# ── 회사별 호출 ───────────────────────────────────────────────────────────
# 반환: {"text": 답(없으면 None), "usage": {"input", "output"}, "stop": 종료 사유}. output에는 추론 토큰을 포함한다(과금 기준)


def _b64(page: bytes) -> str:
    return base64.b64encode(page).decode()


def _call_openai(model: str, user_prompt: str, pages: list[bytes]) -> dict:
    from openai import OpenAI

    content = [{"type": "input_text", "text": user_prompt},
               *({"type": "input_image", "image_url": f"data:image/png;base64,{_b64(p)}"} for p in pages)]
    resp = OpenAI().responses.create(
        model=model, instructions=SYSTEM_PROMPT, input=[{"role": "user", "content": content}],
        max_output_tokens=MAX_OUTPUT_TOKENS,
    )
    return {"text": resp.output_text, "stop": resp.status,
            "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}}


def _call_gemini(model: str, user_prompt: str, pages: list[bytes]) -> dict:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    resp = client.models.generate_content(
        model=model, contents=[user_prompt, *(types.Part.from_bytes(data=p, mime_type="image/png") for p in pages)],
        config=types.GenerateContentConfig(system_instruction=SYSTEM_PROMPT, max_output_tokens=MAX_OUTPUT_TOKENS),
    )
    meta = resp.usage_metadata
    stop = resp.candidates[0].finish_reason if resp.candidates else None
    return {"text": resp.text, "stop": str(stop),
            "usage": {"input": meta.prompt_token_count or 0,
                      "output": (meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)}}


def _call_anthropic(model: str, user_prompt: str, pages: list[bytes]) -> dict:
    from pipeline.vision_client import get_client  # 서비스와 같은 클라이언트 — workspace 헤더를 같은 방식으로 붙인다

    content = [*({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": _b64(p)}} for p in pages),
               {"type": "text", "text": user_prompt}]
    # 거절 시 다른 모델로 넘기는 fallbacks는 쓰지 않는다 — 다른 모델의 답이 이 모델의 점수에 섞인다. 거절은 실패로 센다
    resp = get_client().messages.create(
        model=model, max_tokens=MAX_OUTPUT_TOKENS, system=SYSTEM_PROMPT, messages=[{"role": "user", "content": content}],
    )
    text = "".join(block.text for block in resp.content if block.type == "text")
    return {"text": text or None, "stop": resp.stop_reason,
            "usage": {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}}


_CALLERS = {"openai": _call_openai, "gemini": _call_gemini, "anthropic": _call_anthropic}
VERSIONS = ("clean", "planted")  # 깨끗한 판, 결함 판


def fetch(name: str, record: dict, version: str, rep: int, split: str) -> dict:
    """모델 하나·견적서 한 판·회차 하나의 응답. 저장된 것이 있으면 그것을 쓰고, 없으면 부르고 저장한다."""
    spec = MODELS[name]
    clean_path, planted_path = image_paths(record["id"])
    # M2(조건 누락)는 공사 정보를 바꾼다 — 결함 판에는 결함 판의 공사 정보를 준다
    info = record["info"] if version == "clean" else record["planted_doc"]["info"]
    user_prompt = build_user_prompt(info)
    pages = split_image(clean_path if version == "clean" else planted_path)
    digest = prompt_hash(user_prompt, pages)
    path = LLM_RUNS_DIR / split / spec["model"] / f"{record['id']}_{version}_r{rep}.json"
    base = {"id": record["id"], "version": version, "rep": rep, "model": spec["model"]}
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:  # 쓰다가 끊긴 파일 — 없는 것으로 보고 다시 부른다
            saved = {}
        if saved.get("prompt_hash") == digest:
            return {**saved, "cached": True}
    try:
        result = _CALLERS[spec["provider"]](spec["model"], user_prompt, pages)
    except Exception as exc:  # 회사마다 예외 종류가 다르다. 한 건의 실패로 전체를 멈추지 않고, 저장하지 않아 다음 실행에서 다시 부른다
        return {**base, "call_error": f"{type(exc).__name__}: {exc}"}
    saved = {
        **base, "prompt_hash": digest, "pages": len(pages),
        "called_at": datetime.datetime.now().isoformat(timespec="seconds"),
        **result, "cost_usd": call_cost(result["usage"], spec["price"]),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")  # 다 쓴 뒤에 이름을 바꾼다 — 쓰는 중에 끊겨도 반쯤 쓰인 응답 파일이 남지 않게
    tmp.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return {**saved, "cached": False}


# ── 집계 ──────────────────────────────────────────────────────────────────

_KEYS = ("찾은_비율", "새_지적_적중률", "깨끗한_판_지적_수_평균")


def score_rep(records: list[dict], answers: dict[tuple[str, str], dict]) -> tuple[list[dict], dict]:
    """한 회차의 채점. answers: (레코드 id, 판) → parse_response의 결과. 반환: (건별 행, 요약).

    읽을 수 없는 판은 지적이 없는 것으로 채점한다 — 결함 판을 못 읽으면 결함을 하나도 못 찾은 것이다.
    """
    rows, failed = [], 0
    for record in records:
        clean, planted = answers[(record["id"], "clean")], answers[(record["id"], "planted")]
        failed += ("error" in clean) + ("error" in planted)
        rows.append({"id": record["id"],
                     **score_record(record["planted"], clean.get("findings", []), planted.get("findings", [])),
                     "errors": [a["error"] for a in (clean, planted) if "error" in a]})
    return rows, {**summarize(rows), "실패한_판": failed}


def aggregate_reps(summaries: list[dict]) -> dict:
    """회차별 요약을 평균과 최소~최대로 묶는다. 값이 없는 회차(새 지적이 하나도 없어 적중률이 없는 회차)는 뺀다."""
    out = {"회차": len(summaries)}
    for key in _KEYS:
        values = [s[key] for s in summaries if s.get(key) is not None]
        out[key] = {"평균": statistics.mean(values), "최소": min(values), "최대": max(values)} if values else None
    for key in ("찾은_결함", "심은_결함", "새_지적", "새_지적_적중", "실패한_판"):
        out[key] = [s.get(key, 0) for s in summaries]
    return out


def _fmt(stat: dict | None, pct: bool = True) -> str:
    if stat is None:
        return "-"
    show = (lambda v: f"{v:.0%}") if pct else (lambda v: f"{v:.1f}")
    return show(stat["평균"]) if stat["최소"] == stat["최대"] else f"{show(stat['평균'])} ({show(stat['최소'])}~{show(stat['최대'])})"


def print_model_report(name: str, per_rep: list[dict], calls: list[dict]) -> None:
    spec, agg = MODELS[name], aggregate_reps(per_rep)
    cost = sum(c["cost_usd"] for c in calls)
    new_cost = sum(c["cost_usd"] for c in calls if not c["cached"])
    print(f"\n=== {name} ({spec['model']}) — 호출 {len(calls)}건, "
          f"평균 토큰 입력 {statistics.mean(c['usage']['input'] for c in calls):.0f}/"
          f"출력 {statistics.mean(c['usage']['output'] for c in calls):.0f}, 비용 ${cost:.2f} (이번 실행 ${new_cost:.2f})")
    print(f"  찾은 비율: {_fmt(agg['찾은_비율'])}  — 회차별 {agg['찾은_결함']} / 심은 결함 {agg['심은_결함'][0]}")
    print(f"  새 지적의 적중률: {_fmt(agg['새_지적_적중률'])}  — 회차별 새 지적 {agg['새_지적']}, 적중 {agg['새_지적_적중']}")
    print(f"  깨끗한 판의 지적 수: 견적서당 평균 {_fmt(agg['깨끗한_판_지적_수_평균'], pct=False)}개")
    if any(agg["실패한_판"]):
        print(f"  답을 읽을 수 없는 판: 회차별 {agg['실패한_판']}")
    by_type: dict[str, list[int]] = {}
    for summary in per_rep:
        for t, v in summary["유형별"].items():
            found, planted = by_type.setdefault(t, [0, 0])
            by_type[t] = [found + v["찾음"], planted + v["심음"]]
    print("  유형별 (찾음/심음, 전 회차 합): " + "  ".join(f"{t} {f}/{p}" for t, (f, p) in by_type.items()))


# ── 실행 ──────────────────────────────────────────────────────────────────


def run(split: str, names: list[str], repeats: int, only: list[str] | None = None) -> None:
    records = [r for r in load_records(split) if not only or r["id"] in only]
    if not records:
        raise SystemExit(f"고른 레코드가 {split} 세트에 없습니다: {only}")
    missing = [MODELS[n]["key"] for n in names if not os.environ.get(MODELS[n]["key"])]
    if missing:
        raise SystemExit(f"키가 없습니다: {missing} — .env에 넣거나 --models로 모델을 고르세요")

    report = {"split": split, "repeats": repeats, "records": [r["id"] for r in records],
              "system_prompt_hash": hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:16], "models": {}}
    for name in names:
        jobs = [(record, version, rep) for record in records for version in VERSIONS for rep in range(1, repeats + 1)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
            calls = list(pool.map(lambda job: fetch(name, job[0], job[1], job[2], split), jobs))
        errors = [f"{c['id']} {c['version']} r{c['rep']}: {c['call_error']}" for c in calls if "call_error" in c]
        if errors:
            # 호출 오류(한도 초과, 네트워크, 잘못된 모델 ID)는 모델의 오답이 아니다. 채점하면 찾은 비율이 깎여 보이므로
            # 이 모델은 채점하지 않는다. 받은 응답은 저장돼 있어서 다시 실행하면 실패한 건만 다시 부른다
            print(f"\n=== {name} ({MODELS[name]['model']}) — 호출 오류 {len(errors)}건, 채점하지 않음. 다시 실행하세요",
                  *errors, sep="\n    ")
            report["models"][name] = {"model": MODELS[name]["model"], "call_errors": errors}
            continue
        per_rep, rows_by_rep, findings_by_rep = [], {}, {}
        for rep in range(1, repeats + 1):
            answers = {(c["id"], c["version"]): parse_response(c["text"]) for c in calls if c["rep"] == rep}
            rows, summary = score_rep(records, answers)
            per_rep.append(summary)
            rows_by_rep[rep] = rows
            findings_by_rep[rep] = {f"{rid}_{version}": answer for (rid, version), answer in answers.items()}
        print_model_report(name, per_rep, calls)
        report["models"][name] = {
            "model": MODELS[name]["model"], "summary_per_rep": per_rep, "rows_per_rep": rows_by_rep,
            "findings_per_rep": findings_by_rep, "cost_usd": sum(c["cost_usd"] for c in calls), "call_errors": [],
        }

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = LLM_RUNS_DIR / f"{split}_{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({**report, "run_at": stamp}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[OK] {len(records)}건 × 2판 × {repeats}회 × {len(names)}모델 → {path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", choices=["dev", "eval"], default="dev")
    parser.add_argument("--final", action="store_true", help="평가용(eval) 세트 실행 확인 — 마지막 비교 때 한 번만")
    parser.add_argument("--models", nargs="+", choices=list(MODELS), default=list(MODELS))
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--records", nargs="+", help="이 id의 견적서만 (예: rk-005)")
    parser.add_argument("--check", action="store_true", help="키와 모델 ID만 확인 (과금 없음)")
    parser.add_argument("--dry-run", action="store_true", help="프롬프트와 이미지 조각 수만 출력 (과금 없음)")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    if args.check:
        check_models(args.models)
        return
    require_final(parser, args)
    if args.repeats < 1:
        parser.error("--repeats는 1 이상")
    if args.dry_run:
        records = [r for r in load_records(args.split) if not args.records or r["id"] in args.records]
        print(SYSTEM_PROMPT, "\n\n---\n", build_user_prompt(records[0]["info"]), "\n\n---", sep="")
        for record in records:
            sizes = [len(split_image(p)) for p in image_paths(record["id"])]
            print(f"{record['id']}: 깨끗한 판 {sizes[0]}장, 결함 판 {sizes[1]}장")
        calls = len(records) * len(VERSIONS) * args.repeats * len(args.models)
        print(f"호출 수: {len(records)}건 × 2판 × {args.repeats}회 × {len(args.models)}모델 = {calls}회")
        return
    run(args.split, args.models, args.repeats, args.records)


if __name__ == "__main__":
    main()
