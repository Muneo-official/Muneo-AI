"""파싱 전 이미지 전처리 — 리사이즈와 세로 청크 분할.

pipeline/reference/parse_estimates.py의 _resize_for_parse()/_split_image_vertically()를
이관했다. 세로로 긴 견적서 이미지를 통째로 한 번에 Vision API에 보내면 카테고리 출력이
불안정해진다는 게 실측으로 확인됐다(pipeline/results/prompt_category_fix.md의 "1차 시도
(실패)" 참고 — 731×3050px 이미지를 안 쪼개고 보냈다가 "건기공사"/"샷호공사" 같은 오타성
출력이 났다). 그래서 이 전처리 단계는 선택이 아니라 필수다.
"""

import io

from PIL import Image

MAX_PARSE_WIDTH = 1400          # 파싱용 최대 너비(px) — 토큰 절약
SPLIT_HEIGHT_THRESHOLD = 3000   # 이 픽셀 이상이면 분할
CHUNK_HEIGHT = 2000              # 청크 1개 높이
CHUNK_OVERLAP = 200              # 청크 간 겹침 (행 잘림 방지, pipeline.parsing.merge_chunk_results가 중복 제거)


def resize_for_parse(img: Image.Image) -> Image.Image:
    """MAX_PARSE_WIDTH를 초과하면 비율 유지하며 축소."""
    w, h = img.size
    if w <= MAX_PARSE_WIDTH:
        return img
    new_h = int(h * MAX_PARSE_WIDTH / w)
    return img.resize((MAX_PARSE_WIDTH, new_h), Image.LANCZOS)


def split_vertically(img: Image.Image) -> list[Image.Image]:
    """세로로 긴 이미지를 CHUNK_HEIGHT 크기로 분할 (CHUNK_OVERLAP 겹침)."""
    w, h = img.size
    chunks = []
    top = 0
    while top < h:
        bottom = min(top + CHUNK_HEIGHT, h)
        chunks.append(img.crop((0, top, w, bottom)))
        if bottom == h:
            break
        top += CHUNK_HEIGHT - CHUNK_OVERLAP
    return chunks


def _prepare_chunks(img: Image.Image) -> list[bytes]:
    img = resize_for_parse(img)
    _, h = img.size
    pieces = [img] if h <= SPLIT_HEIGHT_THRESHOLD else split_vertically(img)

    out = []
    for piece in pieces:
        buf = io.BytesIO()
        piece.save(buf, format="PNG")
        out.append(buf.getvalue())
    return out


def prepare_chunks(image_path: str) -> list[bytes]:
    """이미지를 로드·리사이즈·(필요시) 분할하여 PNG bytes 리스트로 반환."""
    return _prepare_chunks(Image.open(image_path))


def prepare_chunks_from_bytes(raw: bytes) -> list[bytes]:
    """업로드된 이미지 bytes(파일 저장 없이)를 리사이즈·(필요시) 분할하여 PNG bytes 리스트로 반환.

    risk_detector처럼 파일시스템에 먼저 안 쓰고 요청 바디에서 바로 받은 이미지를
    다루는 경로용 — 리사이즈/분할 로직은 prepare_chunks()와 완전히 동일하다.
    """
    return _prepare_chunks(Image.open(io.BytesIO(raw)).convert("RGB"))
