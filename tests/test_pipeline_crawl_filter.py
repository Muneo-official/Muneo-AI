import pathlib
import re

from pipeline.crawl_filter import (
    KNOWN_BOILERPLATE_HASHES,
    compute_image_hash,
    find_boilerplate_hashes,
    is_boilerplate,
)


def test_known_boilerplate_hashes_are_valid_sha256_hex():
    assert len(KNOWN_BOILERPLATE_HASHES) == 14
    assert all(re.fullmatch(r"[0-9a-f]{64}", h) for h in KNOWN_BOILERPLATE_HASHES)


def test_compute_image_hash_is_deterministic(tmp_path: pathlib.Path):
    path = tmp_path / "a.bin"
    path.write_bytes(b"hello world")
    assert compute_image_hash(str(path)) == compute_image_hash(str(path))


def test_compute_image_hash_differs_for_different_content(tmp_path: pathlib.Path):
    path_a = tmp_path / "a.bin"
    path_b = tmp_path / "b.bin"
    path_a.write_bytes(b"content A")
    path_b.write_bytes(b"content B")
    assert compute_image_hash(str(path_a)) != compute_image_hash(str(path_b))


def test_is_boilerplate_true_for_known_hash(tmp_path: pathlib.Path):
    path = tmp_path / "logo.bin"
    path.write_bytes(b"logo bytes")
    known = frozenset({compute_image_hash(str(path))})
    assert is_boilerplate(str(path), known)


def test_is_boilerplate_false_for_unique_content(tmp_path: pathlib.Path):
    path = tmp_path / "unique.bin"
    path.write_bytes(b"this content never appears elsewhere")
    assert not is_boilerplate(str(path), KNOWN_BOILERPLATE_HASHES)


def test_find_boilerplate_hashes_detects_repeated_files(tmp_path: pathlib.Path):
    # 서로 다른 게시글 3개에서 완전히 동일한 파일이 반복되는 상황을 재현
    repeated_content = b"same logo bytes"
    paths = []
    for i in range(3):
        p = tmp_path / f"article_{i}_logo.bin"
        p.write_bytes(repeated_content)
        paths.append(str(p))

    unique_path = tmp_path / "real_estimate.bin"
    unique_path.write_bytes(b"unique estimate table content")
    paths.append(str(unique_path))

    found = find_boilerplate_hashes(paths, min_occurrences=3)
    assert compute_image_hash(str(paths[0])) in found
    assert compute_image_hash(str(unique_path)) not in found


def test_find_boilerplate_hashes_respects_min_occurrences(tmp_path: pathlib.Path):
    repeated_content = b"appears only twice"
    for i in range(2):
        (tmp_path / f"{i}.bin").write_bytes(repeated_content)
    paths = [str(tmp_path / f"{i}.bin") for i in range(2)]

    found = find_boilerplate_hashes(paths, min_occurrences=3)
    assert found == set()
