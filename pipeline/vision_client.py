"""실시간 Vision API 호출 — 이미지 1장(필요시 청크 분할)을 견적 데이터로 변환한다.

pipeline/tool_schema.py(tool use, category enum 강제)로 API를 호출하고,
pipeline/image_prep.py(전처리)·pipeline/parsing.py(청크 병합)와 이어붙인다.

Anthropic API를 실제로 호출하는 모듈이라 API 키 없이는 단위테스트할 수 없다 — 순수 로직
(image_prep, parsing, validators, categories, routing)은 전부 별도 모듈로 분리해뒀고,
이 모듈의 실동작 검증은 pipeline/results/*.md에 실제 호출 기록으로 남겼다.
"""

import base64
import hashlib
import json
import os
import time
from dataclasses import dataclass

import anthropic

from pipeline.crawl_filter import is_boilerplate
from pipeline.image_prep import (
    CHUNK_HEIGHT,
    CHUNK_OVERLAP,
    MAX_PARSE_WIDTH,
    SPLIT_HEIGHT_THRESHOLD,
    prepare_chunks,
)
from pipeline.parsing import merge_chunk_results
from pipeline.tool_schema import (
    ESTIMATE_TOOL,
    RISK_ESTIMATE_TOOL,
    RISK_TOOL_USE_INSTRUCTIONS,
    TOOL_NAME,
    TOOL_USE_INSTRUCTIONS,
)

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192
# 리스크 진단(실시간) 파싱 모델. 크롤링 수집은 MODEL 그대로다.
# Sonnet 4.6 → 5.5 교체: S3 비용 −33%, 응답 −69%, 정답률 중앙값 82% → 100%
# (pipeline/results/risk_detector_model_comparison.md, 측정 전 확정한 기준 6개 통과)
RISK_MODEL = "claude-sonnet-5-5"
# 강제 도구 호출(tool_choice: tool/any)을 400으로 거부하는 모델 (claude-api 스킬 모델표, 2026-09-25 기준)
_NO_FORCED_TOOL_MODELS = {"claude-sonnet-5-5"}
# 서버 측 거부 대체(fallbacks: "default")를 켜는 모델. 안전 분류기가 요청을 거부하면 서버가 다른 모델로 다시 돌린다.
# Sonnet 5.5에서는 "cyber"·"frontier_llm" 거부만 대체되고 "general_harms" 등은 그대로 거부로 온다(claude-api 스킬
# model-migration.md, Safeguards and fallback). 견적서엔 거의 해당 없지만 Anthropic 권장 기본값이라 켠다.
_SERVER_FALLBACK_MODELS = {"claude-sonnet-5-5"}
_SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"

_client: anthropic.Anthropic | None = None
_async_client: anthropic.AsyncAnthropic | None = None


def _default_headers() -> dict | None:
    """identity-linked API 키(여러 workspace에 걸친 조직 계정 키)는 어느 workspace로
    요청을 실행할지 anthropic-workspace-id 헤더로 명시해야 한다 — 특히 Batches API에서
    "anthropic-workspace-id is required..." 400 에러로 드러난다. .env에
    ANTHROPIC_WORKSPACE_ID가 있으면 자동으로 헤더에 실어 보낸다.
    """
    workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
    return {"anthropic-workspace-id": workspace_id} if workspace_id else None


def get_client() -> anthropic.Anthropic:
    """지연 초기화 — 이 모듈을 import하는 것만으로 ANTHROPIC_API_KEY를 요구하지 않는다."""
    global _client
    if _client is None:
        _client = anthropic.Anthropic(default_headers=_default_headers())
    return _client


def get_async_client() -> anthropic.AsyncAnthropic:
    """실시간 경로(risk_detector)용 비동기 클라이언트 — 청크 호출을 동시에 보내도 스레드를 점유하지 않는다.

    동기 클라이언트를 run_in_threadpool로 병렬화하면 호출 하나가 응답(수십 초)까지 스레드 하나를
    붙잡아서, 동시 요청 × 청크 수가 기본 threadpool(40)을 금방 넘는다
    (docs/RISK_DETECTOR_PERF_COST_LOG.md 베이스라인 섹션).
    """
    global _async_client
    if _async_client is None:
        _async_client = anthropic.AsyncAnthropic(default_headers=_default_headers())
    return _async_client


def build_api_params(image_bytes: bytes) -> dict:
    """크롤링 수집(배치)·동기 호출용 API 파라미터. tool use로 category를 enum 강제한다."""
    return _build_image_params(image_bytes, ESTIMATE_TOOL)


def build_risk_api_params(image_bytes: bytes, model: str | None = None) -> dict:
    """리스크 진단(실시간) 전용 — 출력 스키마는 RISK_ESTIMATE_TOOL(quantity 제외, code 필수, 필름 분류 규칙), 지시문은 RISK_TOOL_USE_INSTRUCTIONS(소계 행·금액 없는 행 포함)를 쓴다.

    model을 안 주면 RISK_MODEL(호출 시점의 모듈 값)을 쓴다 — 벤치 서버가 --model로 바꿔 모델을 비교한다.
    """
    model = model or RISK_MODEL
    params = _build_image_params(image_bytes, RISK_ESTIMATE_TOOL, model, RISK_TOOL_USE_INSTRUCTIONS)
    if model in _NO_FORCED_TOOL_MODELS:
        # 이 모델들은 강제 도구 호출(tool_choice: tool)이 400이다. 지시문이 이미 "record_estimate 도구를 호출해"라고
        # 명시하므로 auto로 두고, 도구를 안 부른 호출은 VisionCallResult.tool_called로 드러낸다.
        # strict는 켜지 않는다 — 스키마(additionalProperties·required)가 바뀌면 모델 비교에 변수가 하나 더 생긴다.
        params["tool_choice"] = {"type": "auto"}
        # thinking이 기본으로 켜져 있어 그대로 두면 생각 토큰이 출력으로 과금된다. 표 옮겨 적기에 생각은 필요 없다.
        params["thinking"] = {"type": "between_tools"}
    if model in _SERVER_FALLBACK_MODELS:
        # client.beta.messages 대신 일반 messages.create + extra_headers/extra_body로 보낸다 — 벤치 서버의 캡처·mock이
        # client.messages.create만 감싸고 있어서, beta 경로로 바꾸면 벤치가 조용히 실제 API를 우회하거나 깨진다.
        params["extra_headers"] = {"anthropic-beta": _SERVER_FALLBACK_BETA}
        params["extra_body"] = {"fallbacks": "default"}
    return params


def _risk_parse_version() -> str:
    """리스크 진단 파싱 결과 캐시(RiskParseCacheRepository)의 버전 — 이 값이 바뀌면 기존 캐시는 전부 무시된다.

    같은 이미지라도 모델·출력 스키마·지시문·청크 분할이 바뀌면 파싱 결과(특히 경계 공종 분류)가 달라진다
    (pipeline/results/risk_detector_cost_optimization.md). 그래서 이것들을 해시해 캐시 키에 넣는다 —
    바꾸면 자동으로 새로 파싱되고, 안 바꾸면 같은 이미지는 계속 같은 결과를 받는다.

    모델별 요청 형식(tool_choice·thinking·fallbacks 등, build_risk_api_params가 붙이는 것)도 결과를 바꾸므로 함께
    해시한다 — 이미지 데이터만 빼고 실제로 보내는 요청 그대로.

    병합 로직(pipeline.parsing.merge_chunk_results)은 코드라 해시로 못 잡는다 — 결과가 달라지게 고치면
    _PARSE_LOGIC_REVISION을 올린다.
    """
    request = build_risk_api_params(b"", RISK_MODEL)
    request.pop("messages")  # 이미지 데이터 — 지시문은 instructions로 따로 넣는다
    spec = {
        "request": request,  # model·max_tokens·tools·tool_choice·thinking·extra_headers·extra_body
        "instructions": RISK_TOOL_USE_INSTRUCTIONS,
        "chunking": [MAX_PARSE_WIDTH, SPLIT_HEIGHT_THRESHOLD, CHUNK_HEIGHT, CHUNK_OVERLAP],
        "logic_revision": _PARSE_LOGIC_REVISION,
    }
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


_PARSE_LOGIC_REVISION = 2  # 2: 청크 중복 열쇠에서 공종을 뺌, 소계 행과 금액 없는 행을 남김


def _build_image_params(image_bytes: bytes, tool: dict, model: str = MODEL, instructions: str = TOOL_USE_INSTRUCTIONS) -> dict:
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "tools": [tool],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(image_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": instructions},
            ],
        }],
    }


# 요청을 만드는 함수(_build_image_params)가 정의된 뒤에 계산해야 한다
RISK_PARSE_VERSION = _risk_parse_version()


def build_pdf_api_params(pdf_bytes: bytes) -> dict:
    """PDF 첨부 견적서용 API 파라미터. 이미지처럼 페이지별로 쪼개 보낼 필요 없이
    Claude가 PDF를 문서 그대로(멀티페이지 포함) 받아 한 번에 파싱한다."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "tools": [ESTIMATE_TOOL],
        "tool_choice": {"type": "tool", "name": TOOL_NAME},
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_bytes).decode("utf-8"),
                    },
                },
                {"type": "text", "text": TOOL_USE_INSTRUCTIONS},
            ],
        }],
    }


@dataclass
class VisionCallResult:
    """Vision 호출 1회의 파싱 결과 + 소요시간·토큰 사용량.

    실시간 경로(risk_detector)의 응답시간·비용을 계측하려고 분리했다
    (docs/RISK_DETECTOR_PERF_COST_LOG.md). 로깅은 호출자가 한다 — pipeline이
    app.core.logging에 의존하지 않도록.
    """

    result: dict
    latency_s: float
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    # 도구를 안 부르고 글로만 답했는지 — 강제 도구 호출이 안 되는 모델(auto)에서 파싱 실패가 조용히 "견적서 아님"으로
    # 바뀌는 걸 구분하려고 둔다
    tool_called: bool = True
    # "refusal"이면 안전 분류기 거부 — 같은 요청을 다시 보내도 또 거부되므로 재시도 대상이 아니다
    stop_reason: str | None = None
    # 실제로 응답한 모델 — 거부 대체(fallbacks)가 돌면 요청 모델과 달라진다
    model: str | None = None


def _to_call_result(response, latency_s: float) -> VisionCallResult:
    result = {"is_estimate": False}
    tool_called = False
    for block in response.content:
        if block.type == "tool_use":
            result = block.input
            tool_called = True
            break

    usage = response.usage
    return VisionCallResult(
        result=result,
        latency_s=latency_s,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
        tool_called=tool_called,
        stop_reason=getattr(response, "stop_reason", None),
        model=getattr(response, "model", None),
    )


def call_vision_api_with_usage(
    image_bytes: bytes, client: anthropic.Anthropic | None = None
) -> VisionCallResult:
    """call_vision_api()와 같은 파싱을 하되, 소요시간과 response.usage를 같이 반환한다."""
    client = client or get_client()
    started = time.perf_counter()
    response = client.messages.create(**build_api_params(image_bytes))
    return _to_call_result(response, time.perf_counter() - started)


async def acall_vision_api_with_usage(
    image_bytes: bytes, client: anthropic.AsyncAnthropic | None = None
) -> VisionCallResult:
    """리스크 진단(실시간) 전용 비동기 호출 — 결과 형태는 call_vision_api_with_usage()와 같다.

    요청은 build_risk_api_params()로 보낸다(unit·quantity를 뺀 출력 스키마). 이 함수는 리스크 진단만 쓰고,
    크롤링 수집은 동기 경로(build_api_params, 전체 스키마)를 그대로 쓴다.
    """
    client = client or get_async_client()
    started = time.perf_counter()
    response = await client.messages.create(**build_risk_api_params(image_bytes))
    return _to_call_result(response, time.perf_counter() - started)


def call_vision_api(image_bytes: bytes, client: anthropic.Anthropic | None = None) -> dict:
    """청크(또는 이미지) 하나를 파싱. tool_use 블록의 input을 그대로 반환한다."""
    return call_vision_api_with_usage(image_bytes, client).result


def parse_image(image_path: str, client: anthropic.Anthropic | None = None) -> dict:
    """이미지 1장을 파싱한다. SPLIT_HEIGHT_THRESHOLD를 넘으면 자동으로 청크 분할 후 병합.

    알려진 보일러플레이트(로고·뱃지·완성 견본 사진 등, pipeline/crawl_filter.py)면
    API 호출 없이 즉시 반환한다 — 실제 크롤링 데이터의 40.3%가 이런 반복 파일이었다
    (pipeline/results/crawl_prefilter.md).
    """
    if is_boilerplate(image_path):
        return {"is_estimate": False}

    client = client or get_client()
    chunks = prepare_chunks(image_path)
    chunk_results = [call_vision_api(c, client) for c in chunks]
    return merge_chunk_results(chunk_results)


def parse_pdf(pdf_path: str, client: anthropic.Anthropic | None = None) -> dict:
    """PDF 첨부 견적서 1건을 파싱한다.

    이미지와 달리 페이지 분할이 필요 없다 — Claude가 멀티페이지 PDF를 문서 하나로 받아
    한 번의 tool_use 호출로 전체 표를 추출한다. 반환 형태는 call_vision_api()와 동일
    (is_estimate/total_cost/line_items가 이미 채워진 dict)이라 merge_and_validate()에
    그대로 넘길 수 있다.
    """
    client = client or get_client()
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()
    response = client.messages.create(**build_pdf_api_params(pdf_bytes))
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"is_estimate": False}


def parse_document(path: str, client: anthropic.Anthropic | None = None) -> dict:
    """확장자로 이미지/PDF를 구분해 알맞은 파서로 위임한다."""
    if path.lower().endswith(".pdf"):
        return parse_pdf(path, client)
    return parse_image(path, client)
