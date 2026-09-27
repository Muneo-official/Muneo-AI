from types import SimpleNamespace

import pytest

from pipeline.vision_client import acall_vision_api_with_usage, call_vision_api, call_vision_api_with_usage


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
        assert params["tool_choice"] == {"type": "tool", "name": "record_estimate"}  # 동기 버전과 같은 파라미터
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))

    call = await acall_vision_api_with_usage(b"img", client)

    assert call.result == tool_input
    assert (call.input_tokens, call.output_tokens) == (1500, 300)
