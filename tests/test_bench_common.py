import io
import json

import pytest
from PIL import Image

from pipeline.image_prep import prepare_chunks_from_bytes
from scripts.bench.common import (
    build_server_record,
    cost_usd,
    count_chunks,
    percentile,
    read_events,
    result_signature,
)
from scripts.bench.report import build_report


def test_cost_usd_prices_cache_tokens_separately():
    # sonnet-4-6: 입력 $3, 출력 $15 / 1M — 캐시 쓰기 1.25배, 읽기 0.1배
    assert cost_usd("claude-sonnet-4-6", 1_000_000, 0) == pytest.approx(3.0)
    assert cost_usd("claude-sonnet-4-6", 0, 1_000_000) == pytest.approx(15.0)
    assert cost_usd("claude-sonnet-4-6", 0, 0, cache_creation_input_tokens=1_000_000) == pytest.approx(3.75)
    assert cost_usd("claude-sonnet-4-6", 0, 0, cache_read_input_tokens=1_000_000) == pytest.approx(0.3)


def test_percentile_interpolates():
    assert percentile([1, 2, 3, 4], 50) == pytest.approx(2.5)
    assert percentile([10], 95) == 10
    assert percentile([1, 2, 3, 4, 5], 100) == 5


@pytest.mark.parametrize("size", [(800, 1200), (1400, 3000), (1400, 3001), (2800, 9000), (731, 3050), (1000, 7000)])
def test_count_chunks_matches_real_image_prep(size):
    buf = io.BytesIO()
    Image.new("RGB", size, "white").save(buf, format="PNG")
    assert count_chunks(*size) == len(prepare_chunks_from_bytes(buf.getvalue()))


def _event(event, ts, rid="r1", **fields):
    return {"timestamp": f"2026-09-27T10:00:{ts:06.3f}+00:00", "event": event, "request_id": rid, **fields}


def test_build_server_record_places_calls_on_request_timeline():
    events = [
        _event("risk_vision_call", 12.0, image_index=0, chunk_index=0, latency_s=10.0, input_tokens=100, output_tokens=10),
        _event("risk_vision_call", 20.0, image_index=0, chunk_index=1, latency_s=8.0, input_tokens=100, output_tokens=10),
        _event("risk_analyze_timing", 21.0, image_count=1, chunk_count=2, line_item_count=5, parse_images_s=18.0,
               vision_latency_sum_s=18.0, rule_analyze_s=0.1, price_check_s=0.9, total_s=19.0,
               input_tokens=200, output_tokens=20, cache_creation_input_tokens=0, cache_read_input_tokens=0),
        _event("http_request", 21.5, duration_ms=20000.0),  # 요청 시작 = 01.5초
    ]

    rec = build_server_record(events)

    assert [c["start_s"] for c in rec["vision_calls"]] == [0.5, 10.5]  # 직렬: 두 번째가 첫 번째 끝난 뒤
    assert rec["server_duration_s"] == 20.0
    assert rec["parse_images_s"] == 18.0


def test_build_server_record_returns_none_without_timing():
    assert build_server_record([_event("http_request", 1.0, duration_ms=10.0)]) is None


def test_read_events_filters_by_request_id_and_offset(tmp_path):
    log = tmp_path / "app.log"
    old = json.dumps(_event("http_request", 1.0, rid="r1", duration_ms=1.0))
    log.write_text(old + "\n", encoding="utf-8")
    offset = log.stat().st_size
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(_event("http_request", 2.0, rid="r1", duration_ms=2.0)) + "\n")
        f.write(json.dumps(_event("http_request", 3.0, rid="other", duration_ms=3.0)) + "\n")
        f.write("not json\n")

    events = read_events(log, {"r1"}, offset)

    assert [e["duration_ms"] for e in events["r1"]] == [2.0]


def test_result_signature_ignores_item_order_and_counts_normal_items():
    body = {"report": {
        "summary": {"total_risk_items": 2, "chips": {"누락": 1, "중복": 0, "불분명": 1}},
        "process_sections": [
            {"process": "도배", "items": [
                {"status": "불분명", "title": "b"}, {"status": "정상", "title": "벽지"}, {"status": "누락", "title": "a"},
            ]},
        ],
    }}

    sig = result_signature(body)

    assert sig["normal_item_count"] == 1
    assert sig["issues"] == [["도배", "누락", "a"], ["도배", "불분명", "b"]]


def test_build_report_embeds_results_and_escapes_script_close(tmp_path):
    result = tmp_path / "latency_x.json"
    result.write_text(json.dumps({"kind": "latency", "label": "</script><b>x", "requests": [], "cases": []}), encoding="utf-8")

    html = build_report([result])

    assert '"__BENCH_DATA__"' not in html
    assert "<\\/script><b>x" in html
    assert html.count("</script>") == 1
