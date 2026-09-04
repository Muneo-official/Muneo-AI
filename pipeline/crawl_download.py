"""이미지 다운로드 — requests만 쓰는 순수 네트워크 로직 .

브라우저로 이미 URL을 수집한 뒤에는, 이미지 자체는 requests로 바로 받아올 수 있다.

받은 바이트를 디스크에 쓰기 전에 알려진 보일러플레이트 해시(pipeline/crawl_filter.py)와
대조해 걸러낸다 — URL만으로는 로고·뱃지 같은 반복 이미지를 구분할 수 없는데(파일명이
매번 랜덤 인코딩됨), 파일 내용은 여러 게시글에 걸쳐 바이트 단위로 완전히 동일하다.
"""

import hashlib
from pathlib import Path
from urllib.parse import urlparse

import requests

from pipeline.crawl_filter import KNOWN_BOILERPLATE_HASHES

HEADERS = {
    "Referer": "https://cafe.naver.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _extract_ext(url: str) -> str:
    path = urlparse(url).path
    ext = Path(path).suffix.lstrip(".")
    return ext if ext else "jpg"


def download_images(image_urls: list[str], article_dir: Path, folder_name: str) -> list[Path]:
    """image_urls를 순서대로 받아 article_dir에 {folder_name}_{idx}.{ext}로 저장한다.

    개별 다운로드 실패는 건너뛰고 계속 진행한다 — 이미지 하나가 만료/삭제됐다고
    나머지 이미지까지 못 받으면 안 된다. 알려진 보일러플레이트(로고·뱃지 등)와 바이트
    단위로 동일한 이미지는 저장하지 않는다.

    저장된 이미지에만 순번을 매긴다 — 보일러플레이트로 걸러진 이미지는 인덱스를
    차지하지 않는다.
    """
    article_dir = Path(article_dir)
    article_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    for url in image_urls:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=10)
            resp.raise_for_status()
        except requests.RequestException:
            continue

        if hashlib.sha256(resp.content).hexdigest() in KNOWN_BOILERPLATE_HASHES:
            continue

        ext = _extract_ext(url)
        out_path = article_dir / f"{folder_name}_{len(saved)}.{ext}"
        out_path.write_bytes(resp.content)
        saved.append(out_path)

    return saved
