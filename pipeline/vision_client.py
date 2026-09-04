"""실시간 Vision API 호출 — 이미지 1장(필요시 청크 분할)을 견적 데이터로 변환한다.

pipeline/tool_schema.py(tool use, category enum 강제)로 API를 호출하고,
pipeline/image_prep.py(전처리)·pipeline/parsing.py(청크 병합)와 이어붙인다.

Anthropic API를 실제로 호출하는 모듈이라 API 키 없이는 단위테스트할 수 없다 — 순수 로직
(image_prep, parsing, validators, categories, routing)은 전부 별도 모듈로 분리해뒀고,
이 모듈의 실동작 검증은 pipeline/results/*.md에 실제 호출 기록으로 남겼다.
"""

import base64
import os

import anthropic

from pipeline.crawl_filter import is_boilerplate
from pipeline.image_prep import prepare_chunks
from pipeline.parsing import merge_chunk_results
from pipeline.tool_schema import ESTIMATE_TOOL, TOOL_NAME, TOOL_USE_INSTRUCTIONS

MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8192

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    """지연 초기화 — 이 모듈을 import하는 것만으로 ANTHROPIC_API_KEY를 요구하지 않는다.

    identity-linked API 키(여러 workspace에 걸친 조직 계정 키)는 어느 workspace로
    요청을 실행할지 anthropic-workspace-id 헤더로 명시해야 한다 — 특히 Batches API에서
    "anthropic-workspace-id is required..." 400 에러로 드러난다. .env에
    ANTHROPIC_WORKSPACE_ID가 있으면 자동으로 헤더에 실어 보낸다.
    """
    global _client
    if _client is None:
        workspace_id = os.environ.get("ANTHROPIC_WORKSPACE_ID")
        default_headers = {"anthropic-workspace-id": workspace_id} if workspace_id else None
        _client = anthropic.Anthropic(default_headers=default_headers)
    return _client


def build_api_params(image_bytes: bytes) -> dict:
    """실시간·배치 공용 API 파라미터. tool use로 category를 enum 강제한다."""
    return {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "tools": [ESTIMATE_TOOL],
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


def call_vision_api(image_bytes: bytes, client: anthropic.Anthropic | None = None) -> dict:
    """청크(또는 이미지) 하나를 파싱. tool_use 블록의 input을 그대로 반환한다."""
    client = client or get_client()
    response = client.messages.create(**build_api_params(image_bytes))
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return {"is_estimate": False}


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
