from types import SimpleNamespace

import pytest

from pipeline.tool_schema import ESTIMATE_TOOL, RISK_ESTIMATE_TOOL
from pipeline.vision_client import (
    acall_vision_api_with_usage,
    build_api_params,
    build_risk_api_params,
    call_vision_api,
    call_vision_api_with_usage,
)


def _fake_client(content, usage):
    response = SimpleNamespace(content=content, usage=usage)
    return SimpleNamespace(messages=SimpleNamespace(create=lambda **params: response))


def _usage(**overrides):
    fields = dict(input_tokens=1500, output_tokens=300, cache_creation_input_tokens=None, cache_read_input_tokens=None)
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_with_usage_returns_tool_input_and_token_counts():
    tool_input = {"is_estimate": True, "total_cost": 100, "line_items": []}
    client = _fake_client([SimpleNamespace(type="tool_use", input=tool_input)], _usage())

    call = call_vision_api_with_usage(b"img", client)

    assert call.result == tool_input
    assert call.input_tokens == 1500
    assert call.output_tokens == 300
    assert call.latency_s >= 0


def test_with_usage_treats_missing_cache_fields_as_zero():
    client = _fake_client([SimpleNamespace(type="tool_use", input={})], SimpleNamespace(input_tokens=1, output_tokens=1))

    call = call_vision_api_with_usage(b"img", client)

    assert call.cache_creation_input_tokens == 0
    assert call.cache_read_input_tokens == 0


def test_with_usage_reads_cache_tokens_when_present():
    client = _fake_client(
        [SimpleNamespace(type="tool_use", input={})],
        _usage(cache_creation_input_tokens=0, cache_read_input_tokens=1200),
    )

    assert call_vision_api_with_usage(b"img", client).cache_read_input_tokens == 1200


def test_call_vision_api_keeps_returning_plain_result():
    client = _fake_client([SimpleNamespace(type="text", text="no tool")], _usage())

    assert call_vision_api(b"img", client) == {"is_estimate": False}


@pytest.mark.asyncio
async def test_async_version_returns_same_shape():
    tool_input = {"is_estimate": True, "line_items": []}
    response = SimpleNamespace(content=[SimpleNamespace(type="tool_use", input=tool_input)], usage=_usage())

    async def create(**params):
        assert params["tool_choice"] == {"type": "tool", "name": "record_estimate"}
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))

    call = await acall_vision_api_with_usage(b"img", client)

    assert call.result == tool_input
    assert (call.input_tokens, call.output_tokens) == (1500, 300)


def test_risk_params_differ_from_collection_params_only_in_tool_schema():
    risk = build_risk_api_params(b"img")
    collection = build_api_params(b"img")

    assert risk["tools"] == [RISK_ESTIMATE_TOOL]
    assert collection["tools"] == [ESTIMATE_TOOL]
    # 모델·max_tokens·tool_choice·메시지(이미지+지시문)는 같다 — 결과 차이를 스키마 변경 하나로만 설명할 수 있게
    assert {k: v for k, v in risk.items() if k != "tools"} == {k: v for k, v in collection.items() if k != "tools"}


def test_sync_call_keeps_collection_schema():
    sent = {}

    def create(**params):
        sent.update(params)
        return SimpleNamespace(content=[], usage=_usage())

    call_vision_api_with_usage(b"img", SimpleNamespace(messages=SimpleNamespace(create=create)))

    assert sent["tools"] == [ESTIMATE_TOOL]


@pytest.mark.asyncio
async def test_async_call_sends_risk_schema():
    sent = {}

    async def create(**params):
        sent.update(params)
        return SimpleNamespace(content=[], usage=_usage())

    await acall_vision_api_with_usage(b"img", SimpleNamespace(messages=SimpleNamespace(create=create)))

    assert sent["tools"] == [RISK_ESTIMATE_TOOL]
