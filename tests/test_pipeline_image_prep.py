import pathlib

from PIL import Image

from pipeline.image_prep import (
    CHUNK_HEIGHT,
    MAX_PARSE_WIDTH,
    SPLIT_HEIGHT_THRESHOLD,
    prepare_chunks,
    resize_for_parse,
    split_vertically,
)


def test_resize_for_parse_leaves_narrow_image_unchanged():
    img = Image.new("RGB", (800, 600))
    resized = resize_for_parse(img)
    assert resized.size == (800, 600)


def test_resize_for_parse_shrinks_wide_image_preserving_ratio():
    img = Image.new("RGB", (2800, 1000))
    resized = resize_for_parse(img)
    assert resized.width == MAX_PARSE_WIDTH
    assert resized.height == 500  # 1000 * (1400/2800)


def test_split_vertically_short_image_not_split_by_caller():
    # split_vertically() 자체는 무조건 나누는 함수 — 문턱값 판단은 prepare_chunks()의 몫.
    img = Image.new("RGB", (700, 1000))
    chunks = split_vertically(img)
    assert len(chunks) == 1
    assert chunks[0].size == (700, 1000)


def test_split_vertically_tall_image_splits_with_overlap():
    # 실제 관측된 크기(731x3050, pipeline/results/prompt_category_fix.md)와 동일한 비율로
    # 재현 — CHUNK_HEIGHT=2000, OVERLAP=200이면 청크 2개(0~2000, 1800~3050)가 나와야 한다.
    img = Image.new("RGB", (731, 3050))
    chunks = split_vertically(img)
    assert len(chunks) == 2
    assert chunks[0].size == (731, CHUNK_HEIGHT)
    assert chunks[1].size == (731, 3050 - 1800)


def test_prepare_chunks_returns_single_chunk_for_short_image(tmp_path: pathlib.Path):
    path = tmp_path / "short.png"
    Image.new("RGB", (700, SPLIT_HEIGHT_THRESHOLD - 1)).save(path)
    chunks = prepare_chunks(str(path))
    assert len(chunks) == 1


def test_prepare_chunks_splits_tall_image(tmp_path: pathlib.Path):
    path = tmp_path / "tall.png"
    Image.new("RGB", (700, SPLIT_HEIGHT_THRESHOLD + 100)).save(path)
    chunks = prepare_chunks(str(path))
    assert len(chunks) == 2
    for c in chunks:
        assert isinstance(c, bytes)
