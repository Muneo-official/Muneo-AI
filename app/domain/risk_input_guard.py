"""리스크 진단 입력 검문 — Vision을 부르기 전에 말이 안 되는 요청을 돌려보낸다.

업로드 이미지는 조각(청크) 하나가 Vision 호출 한 번이라, 장수와 세로 길이에 상한이 없으면 요청 하나가
호출을 수십 번 일으킨다(파일 용량과는 상관없다 — 세로 2만px 흰 이미지는 126KB인데 11조각이다).
그래서 용량과 함께 조각 수를 센다. 이미지는 디코딩하지 않고 헤더의 형식·크기만 읽는다.

상한은 수집한 견적서 이미지 1,222장을 재서 정했다: 용량 최대 4.1MB, 이미지당 조각 최대 7개,
견적서는 대부분 1~2장, 평수는 최대 74평. 휴대폰으로 찍은 원본 사진은 그 자료에 없어 용량·픽셀은 여유를 뒀다.
"""

import io
import warnings
from dataclasses import replace
from typing import get_args

from PIL import Image

from app.domain.risk_constants import SUPPORTED_SPACE_TYPES
from app.schemas.estimate import 건물연식_리터럴, 건물연식_정규화, 지역_리터럴
from app.schemas.risk import AnalyzeRiskCommand
from pipeline.image_prep import chunk_count

MAX_IMAGES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 50_000_000  # 5천만 화소 휴대폰 사진(8160×6120)까지. 디코딩하면 화소당 3바이트를 쓴다
MAX_CHUNKS_PER_IMAGE = 8
MAX_CHUNKS_PER_REQUEST = 20
ALLOWED_IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")

MIN_PYEONG, MAX_PYEONG = 5, 150
MIN_ROOM_COUNT, MAX_ROOM_COUNT = 1, 10
MIN_FLOOR, MAX_FLOOR = -2, 70  # 음수는 지하·반지하. 0층은 없다
MAX_COMPANY_NAME_LENGTH = 50

REGIONS = get_args(지역_리터럴)
BUILDING_AGES = get_args(건물연식_리터럴)


class InputRejected(ValueError):
    """입력 검문에서 막은 요청. 라우터가 422로 돌려주고, reason은 사유별 건수를 로그로 세는 데 쓴다."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def check_upload_count(count: int) -> None:
    if count < 1:
        raise InputRejected("no_image", "최소 1개 이상의 견적서 이미지가 필요합니다.")
    if count > MAX_IMAGES:
        raise InputRejected("too_many_images", f"견적서 이미지는 한 번에 {MAX_IMAGES}장까지 올릴 수 있습니다.")


def check_upload_size(size: int) -> None:
    if size <= 0:
        raise InputRejected("empty_file", "비어 있는 파일이 있습니다. 견적서 이미지를 다시 선택해 주세요.")
    if size > MAX_FILE_BYTES:
        raise InputRejected(
            "file_too_large", f"이미지 한 장은 {MAX_FILE_BYTES // (1024 * 1024)}MB까지 올릴 수 있습니다."
        )


def normalize_form(command: AnalyzeRiskCommand) -> AnalyzeRiskCommand:
    """폼 값을 검사하고, 표기만 다른 건물 연식("20년 이상")은 가견적과 같은 값으로 맞춰 돌려준다."""
    if command.space_type not in SUPPORTED_SPACE_TYPES:
        raise InputRejected("space_type", f"지원하지 않는 공간유형입니다: {command.space_type[:20]}")
    if not MIN_PYEONG <= command.pyeong <= MAX_PYEONG:
        raise InputRejected("pyeong", f"평수는 {MIN_PYEONG}~{MAX_PYEONG}평 사이로 입력해 주세요.")
    if not MIN_ROOM_COUNT <= command.room_count <= MAX_ROOM_COUNT:
        raise InputRejected("room_count", f"방 개수는 {MIN_ROOM_COUNT}~{MAX_ROOM_COUNT}개 사이로 입력해 주세요.")
    if command.floor == 0 or not MIN_FLOOR <= command.floor <= MAX_FLOOR:
        raise InputRejected(
            "floor", f"층수는 지하 {-MIN_FLOOR}층부터 {MAX_FLOOR}층 사이로 입력해 주세요(0층은 없습니다)."
        )
    if command.region not in REGIONS:
        raise InputRejected("region", f"지역은 {', '.join(REGIONS)} 중에서 골라 주세요.")
    building_age = 건물연식_정규화(command.building_age)
    if building_age not in BUILDING_AGES:
        raise InputRejected("building_age", f"건물 연식은 {', '.join(BUILDING_AGES)} 중에서 골라 주세요.")
    company_name = command.company_name.strip()
    if len(company_name) > MAX_COMPANY_NAME_LENGTH:
        raise InputRejected("company_name", f"업체명은 {MAX_COMPANY_NAME_LENGTH}자까지 입력할 수 있습니다.")
    return replace(command, building_age=building_age, company_name=company_name)


def _image_size(raw: bytes) -> tuple[int, int]:
    """형식을 확인하고 (가로, 세로)를 돌려준다. 파일 이름이나 Content-Type은 올리는 쪽이 정하는 값이라 믿지 않는다."""
    try:
        with warnings.catch_warnings():
            # 큰 이미지에 PIL이 내는 경고 — 크기는 아래에서 직접 막는다
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as img:
                image_format, size = img.format, img.size
    except Image.DecompressionBombError:
        raise InputRejected("image_too_large", "이미지가 너무 큽니다. 해상도를 줄여서 다시 올려 주세요.") from None
    except Exception:
        # PIL은 형식에 따라 OSError 말고도 SyntaxError·ValueError 등을 낸다. 못 여는 파일은 어느 쪽이든 이미지가 아니다
        raise InputRejected(
            "not_an_image", "이미지 파일만 올릴 수 있습니다(PNG, JPG, WEBP). PDF는 이미지로 바꿔서 올려 주세요."
        ) from None
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise InputRejected("unsupported_format", "PNG, JPG, WEBP 형식의 이미지만 올릴 수 있습니다.")
    return size


def check_images(image_files: list[bytes]) -> None:
    """장수·용량·형식·크기·조각 수를 본다. 통과한 요청의 Vision 호출은 MAX_CHUNKS_PER_REQUEST번을 넘지 않는다."""
    check_upload_count(len(image_files))
    total_chunks = 0
    for raw in image_files:
        check_upload_size(len(raw))
        width, height = _image_size(raw)
        chunks = chunk_count(width, height)
        if width * height > MAX_IMAGE_PIXELS or chunks > MAX_CHUNKS_PER_IMAGE:
            raise InputRejected(
                "image_too_large", "이미지가 너무 큽니다. 견적서를 여러 장으로 나누거나 해상도를 줄여서 올려 주세요."
            )
        total_chunks += chunks
    if total_chunks > MAX_CHUNKS_PER_REQUEST:
        raise InputRejected(
            "too_many_chunks", "한 번에 분석하기에는 견적서가 너무 깁니다. 이미지를 나눠서 올려 주세요."
        )
