"""실시간 Vision API 호출 — 이미지 1장(필요시 청크 분할)을 견적 데이터로 변환한다.

pipeline/tool_schema.py(tool use, category enum 강제)로 API를 호출하고,
pipeline/image_prep.py(전처리)·pipeline/parsing.py(청크 병합)와 이어붙인다.

Anthropic API를 실제로 호출하는 모듈이라 API 키 없이는 단위테스트할 수 없다 — 순수 로직
(image_prep, parsing, validators, categories, routing)은 전부 별도 모듈로 분리해뒀고,
이 모듈의 실동작 검증은 pipeline/results/*.md에 실제 호출 기록으로 남겼다.
"""

import base64
import os
import time
from dataclasses import dataclass

import anthropic

from pipeline.crawl_filter import is_boilerplate
from pipeline.image_prep import prepare_chunks
from pipeline.parsing import merge_chunk_results
from pipeline.tool_schema import ESTIMATE_TOOL, RISK_ESTIMATE_TOOL, TOOL_NAME, TOOL_USE_INSTRUCTIONS

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192

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


def build_risk_api_params(image_bytes: bytes) -> dict:
    """리스크 진단(실시간) 전용 — 출력 스키마만 RISK_ESTIMATE_TOOL(unit·quantity 제외)로 바꾸고 나머지는 같다."""
    return _build_image_params(image_bytes, RISK_ESTIMATE_TOOL)


def _build_image_params(image_bytes: bytes, tool: dict) -> dict:
    return {
        "model": MODEL,
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
                {"type": "text", "text": TOOL_USE_INSTRUCTIONS},
            ],
        }],
    }


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


def _to_call_result(response, latency_s: float) -> VisionCallResult:
    result = {"is_estimate": False}
    for block in response.content:
        if block.type == "tool_use":
            result = block.input
            break

    usage = response.usage
    return VisionCallResult(
        result=result,
        latency_s=latency_s,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=getattr(usage, "cache_creation_input_tokens", None) or 0,
        cache_read_input_tokens=getattr(usage, "cache_read_input_tokens", None) or 0,
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
