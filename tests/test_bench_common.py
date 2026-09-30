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
    read_events_named,
    result_signature,
)
from scripts.bench.k6_result import levels_from_points, scenario_meta, server_side_stats, thresholds_by_scenario
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


def test_read_events_named_collects_event_regardless_of_request_id(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("\n".join([
        json.dumps(_event("risk_analyze_timing", 1.0, rid="a", total_s=1.0)),
        json.dumps(_event("http_request", 1.1, rid="a", duration_ms=1.0)),
        json.dumps(_event("risk_analyze_timing", 2.0, rid="b", total_s=2.0)),
    ]) + "\n", encoding="utf-8")

    assert [e["total_s"] for e in read_events_named(log, "risk_analyze_timing")] == [1.0, 2.0]


def _point(metric, value, scenario, end_s, ok=True):
    tags = {"scenario": scenario, "case": "S3", "expected_response": "true" if ok else "false"}
    return {"type": "Point", "metric": metric,
            "data": {"time": f"2026-09-27T10:00:{end_s:06.3f}+00:00", "value": value, "tags": tags}}


def test_levels_from_points_uses_end_time_minus_duration_as_start():
    points = [
        _point("http_req_duration", 5000, "c2", 10.0), _point("http_req_failed", 0, "c2", 10.0),
        _point("http_req_duration", 7000, "c2", 13.0), _point("http_req_failed", 0, "c2", 13.0),
        _point("http_req_duration", 100, "c2", 13.5, ok=False), _point("http_req_failed", 1, "c2", 13.5),
        _point("http_req_duration", 999, "setup", 1.0),  # setup()의 /bench/info 요청은 제외
    ]

    level = levels_from_points(points)["c2"]

    assert level["concurrency"] == 2 and level["model"] == "closed"
    assert level["total_requests"] == 3
    assert level["success"] == 2
    assert level["error_rate"] == pytest.approx(0.3333, abs=1e-4)
    assert level["p50_s"] == 6.0  # 실패 요청은 응답시간 통계에서 제외
    start, end = level["window"]
    assert end - start == pytest.approx(8.5)  # 첫 요청 시작(5.0초) ~ 마지막 종료(13.5초)
    assert level["throughput_rpm"] == pytest.approx(2 / 8.5 * 60, abs=0.01)


def test_levels_from_points_counts_dropped_iterations_for_open_model():
    points = [_point("http_req_duration", 1000, "r4", 5.0), _point("dropped_iterations", 1, "r4", 6.0),
              _point("dropped_iterations", 1, "r4", 7.0)]

    level = levels_from_points(points)["r4"]

    assert level["model"] == "open" and level["arrival_rpm"] == 4
    assert level["dropped_iterations"] == 2


def test_scenario_meta_rejects_unknown_names():
    with pytest.raises(ValueError):
        scenario_meta("default")


def test_thresholds_by_scenario_inverts_summary_export_flag():
    # summary-export는 기준을 넘으면(실패) true를 기록한다
    summary = {"metrics": {
        "http_req_duration{scenario:c5}": {"thresholds": {"p(95)<120000": True}},
        "http_req_failed{scenario:c5}": {"thresholds": {"rate<0.01": False}},
        "http_req_duration": {"thresholds": {"p(95)<1": True}},
    }}

    result = thresholds_by_scenario(summary)

    assert result == {"c5": {"http_req_duration{scenario:c5}: p(95)<120000": False,
                             "http_req_failed{scenario:c5}: rate<0.01": True}}


def test_server_side_stats_medians():
    events = [{"parse_images_s": p, "price_check_s": 0.5, "total_s": p + 0.5} for p in (10.0, 20.0, 30.0)]

    stats = server_side_stats(events)

    assert stats == {"requests": 3, "parse_images_p50_s": 20.0, "price_check_p50_s": 0.5, "total_p50_s": 20.5}
    assert server_side_stats([]) is None
