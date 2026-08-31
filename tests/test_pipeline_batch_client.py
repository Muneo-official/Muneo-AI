import pathlib

from PIL import Image

from pipeline.batch_client import RequestMeta, build_batch_requests


def _make_image(path: pathlib.Path, height: int) -> str:
    Image.new("RGB", (700, height)).save(path)
    return str(path)


def test_build_batch_requests_single_image_single_chunk(tmp_path: pathlib.Path):
    img = _make_image(tmp_path / "a.png", height=1000)
    requests, meta = build_batch_requests([("123", [img])])

    assert len(requests) == 1
    assert requests[0]["custom_id"] == "123__0__0"
    assert meta["123__0__0"] == RequestMeta("123", 0, 0)


def test_build_batch_requests_tall_image_produces_multiple_chunks(tmp_path: pathlib.Path):
    img = _make_image(tmp_path / "tall.png", height=3100)  # SPLIT_HEIGHT_THRESHOLD(3000) 초과
    requests, meta = build_batch_requests([("123", [img])])

    assert len(requests) == 2
    assert {r["custom_id"] for r in requests} == {"123__0__0", "123__0__1"}
    assert meta["123__0__1"] == RequestMeta("123", 0, 1)


def test_build_batch_requests_multiple_articles_and_images(tmp_path: pathlib.Path):
    img_a0 = _make_image(tmp_path / "a0.png", height=500)
    img_a1 = _make_image(tmp_path / "a1.png", height=500)
    img_b0 = _make_image(tmp_path / "b0.png", height=500)

    requests, meta = build_batch_requests([
        ("111", [img_a0, img_a1]),
        ("222", [img_b0]),
    ])

    custom_ids = {r["custom_id"] for r in requests}
    assert custom_ids == {"111__0__0", "111__1__0", "222__0__0"}
    assert meta["111__1__0"].article_id == "111"
    assert meta["111__1__0"].image_index == 1


def test_build_batch_requests_params_use_tool_use(tmp_path: pathlib.Path):
    # 실시간 경로(pipeline/vision_client.py)와 동일한 build_api_params를 재사용해야
    # 배치도 category enum 강제가 똑같이 적용된다.
    img = _make_image(tmp_path / "a.png", height=500)
    requests, _ = build_batch_requests([("123", [img])])

    params = requests[0]["params"]
    assert params["tool_choice"] == {"type": "tool", "name": "record_estimate"}
    assert params["tools"][0]["name"] == "record_estimate"
