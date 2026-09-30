from types import SimpleNamespace

import pytest

from pipeline import vision_client
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
        assert params["model"] == vision_client.RISK_MODEL
        return response

    client = SimpleNamespace(messages=SimpleNamespace(create=create))

    call = await acall_vision_api_with_usage(b"img", client)

    assert call.result == tool_input
    assert (call.input_tokens, call.output_tokens) == (1500, 300)


def test_risk_params_differ_from_collection_params_only_in_tool_schema():
    # 같은 모델로 보내면 수집 경로와 스키마만 다르다 (리스크 경로의 모델 교체는 별도 테스트)
    risk = build_risk_api_params(b"img", vision_client.MODEL)
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


def test_risk_params_use_forced_tool_for_models_that_allow_it():
    for model in ("claude-sonnet-4-6", "claude-haiku-4-5"):
        params = build_risk_api_params(b"img", model)
        assert params["tool_choice"] == {"type": "tool", "name": "record_estimate"}
        assert "thinking" not in params
    assert build_risk_api_params(b"img", "claude-haiku-4-5")["model"] == "claude-haiku-4-5"


def test_risk_params_for_model_without_forced_tool_use_auto_and_turn_thinking_off():
    params = build_risk_api_params(b"img", "claude-sonnet-5-5")

    assert params["model"] == "claude-sonnet-5-5"
    assert params["tool_choice"] == {"type": "auto"}  # 강제 호출은 400
    assert params["thinking"] == {"type": "between_tools"}  # 기본 thinking이 출력 토큰으로 과금되지 않게
    assert params["tools"] == [RISK_ESTIMATE_TOOL]  # 스키마는 모델과 무관하게 같다
    # 거부 대체: 일반 messages.create에 헤더·본문으로 실어 보낸다 (벤치 캡처·mock이 messages.create만 감싸서)
    assert params["extra_headers"] == {"anthropic-beta": "server-side-fallback-2026-07-01"}
    assert params["extra_body"] == {"fallbacks": "default"}


def test_production_risk_model_uses_auto_tool_choice_and_server_fallback():
    # 운영 리스크 모델(Sonnet 5.5)은 옵션 없이 호출해도 5.5 요청 형식으로 나간다
    params = build_risk_api_params(b"img")

    assert params["model"] == "claude-sonnet-5-5"
    assert params["tool_choice"] == {"type": "auto"}
    assert params["extra_body"] == {"fallbacks": "default"}
    assert build_api_params(b"img")["model"] == "claude-sonnet-4-6"  # 크롤링 수집은 그대로


def test_server_fallback_only_for_models_that_need_it():
    for model in ("claude-sonnet-4-6", "claude-haiku-4-5"):
        params = build_risk_api_params(b"img", model)
        assert "extra_headers" not in params and "extra_body" not in params


def test_call_result_records_stop_reason_and_serving_model():
    response = SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input={})], usage=_usage(),
        stop_reason="tool_use", model="claude-sonnet-5",
    )
    client = SimpleNamespace(messages=SimpleNamespace(create=lambda **params: response))

    call = call_vision_api_with_usage(b"img", client)

    assert (call.stop_reason, call.model) == ("tool_use", "claude-sonnet-5")


def test_risk_params_follow_module_risk_model_at_call_time(monkeypatch):
    # 벤치 서버가 --model로 모듈 값을 바꾸면 이후 호출부터 그 모델로 나간다
    monkeypatch.setattr(vision_client, "RISK_MODEL", "claude-haiku-4-5")

    assert build_risk_api_params(b"img")["model"] == "claude-haiku-4-5"
    assert build_api_params(b"img")["model"] == vision_client.MODEL  # 수집 경로는 그대로


def test_tool_called_is_false_when_model_answers_in_text():
    client = _fake_client([SimpleNamespace(type="text", text="견적서가 아닙니다")], _usage())

    call = call_vision_api_with_usage(b"img", client)

    assert call.tool_called is False
    assert call.result == {"is_estimate": False}


@pytest.mark.asyncio
async def test_async_call_sends_risk_schema():
    sent = {}

    async def create(**params):
        sent.update(params)
        return SimpleNamespace(content=[], usage=_usage())

    await acall_vision_api_with_usage(b"img", SimpleNamespace(messages=SimpleNamespace(create=create)))

    assert sent["tools"] == [RISK_ESTIMATE_TOOL]
