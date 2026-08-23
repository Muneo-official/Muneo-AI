from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

공종_리터럴 = Literal[
    "도배", "장판", "마루", "주방", "욕실", "전기/조명",
    "목공", "도장", "설비", "창호", "필름", "가구", "마감/공과잡비",
]


도배_범위_리터럴 = Literal["전체", "거실", "침실", "주방"]


class 도배옵션(BaseModel):
    범위: 도배_범위_리터럴 | list[도배_범위_리터럴] = "전체"
    도배지종류: str = "실크벽지"
    초배포함: str | None = None


class 마루옵션(BaseModel):
    자재종류: str | None = None
    범위: str = "전체"
    철거여부: str | None = None


class 욕실옵션(BaseModel):
    개수: int = 1
    크기: str | None = None
    도기교체: str | None = None
    방수포함: str | None = None
    욕조샤워부스: str | None = None
    타일등급: str | None = None


class 주방옵션(BaseModel):
    싱크대교체: str | None = None
    형태: str | None = None
    길이: str | None = None


class EstimateRequest(BaseModel):
    """가견적 생성 요청. 필드명은 기존 estimate_engine 입력 규격을 그대로 따른다."""

    공종: list[공종_리터럴] = Field(default_factory=list)
    시공범위: Literal["전체", "부분"] = "부분"
    공간유형: str = "아파트"
    평수: int = Field(gt=0)
    방개수: int = 3
    지역: Literal["서울", "수도권", "지방"] = "서울"

    건물연식: Literal["신축(3년이하)", "10년이하", "10~20년", "20년이상"] = "10~20년"
    자재등급: Literal["일반", "중급", "고급"] = "중급"
    철거여부: Literal["있음", "없음", "모름"] = "모름"
    층수: int = 1
    엘리베이터: Literal["있음", "없음"] = "있음"
    트럭접근: Literal["가능", "불가(골목·지하)", "모름"] = "가능"
    거주중공사: Literal["거주중", "공실"] = "공실"
    공사시기: Literal["1개월이내", "1~3개월", "3개월이후", "미정"] = "미정"

    도배: 도배옵션 | None = None
    마루: 마루옵션 | None = None
    욕실: 욕실옵션 | None = None
    주방: 주방옵션 | None = None


class 금액범위(BaseModel):
    최소: int
    중간: int
    최대: int


class 참고사례(BaseModel):
    article_id: str | None = None
    지역: str | None = None
    평수: int | None = None
    총금액: int
    평당: int


class EstimateResponse(BaseModel):
    총_견적_범위: 금액범위
    공종별_단가_범위: dict[str, 금액범위]
    공종별_항목_명세: dict[str, list[dict]]
    보정_적용: list[str]
    시공범위: str
    선택_공종: list[str]
    참고_사례_수: int
    참고_사례: list[참고사례]
    검색_쿼리: str
    데이터_부족_공종: list[str] | None = None
    단독시공_주의: str | None = None

    # 재현성 추적용 — 나중에 계수/엔진이 바뀌어도 이 견적이 어떤 버전·어떤 사례로 산출됐는지 역추적 가능
    engine_version: str
    coefficient_version: str
    reference_case_ids: list[str] = Field(default_factory=list)

    # /estimates/save로 그대로 저장할 때 쓰는 1회용 토큰. 클라이언트가 총_견적_범위 등을 직접 조작해서
    # 저장하는 걸 막기 위해, 서버가 계산한 이 결과를 잠깐 캐싱해두고 토큰만 돌려준다.
    estimate_token: str


class EstimateError(BaseModel):
    error: str


class SaveEstimateRequest(BaseModel):
    """generate() 응답에 담긴 estimate_token으로 저장을 요청한다.

    input/result를 클라이언트가 직접 보내지 않는다 — 서버가 /generate 시점에 계산해서 캐싱해둔
    값을 그대로 쓴다. 클라이언트가 총_견적_범위 등을 조작해서 저장하는 걸 막기 위함
    (조작 가능하면 estimate_feedback 기반 정확도 집계도 조작 가능해진다).
    """

    estimate_token: str


class SavedEstimateId(BaseModel):
    id: str


EstimateStatus = Literal["saved", "contracted", "expired"]


class SavedEstimateSummary(BaseModel):
    """목록 조회 응답. result에서 참고_사례/참고_사례_수/검색_쿼리는 repository가 미리 제외한다."""

    id: str
    user_id: str
    created_at: datetime
    input: EstimateRequest
    result: dict
    status: EstimateStatus
    valid_until: datetime


class FeedbackRequest(BaseModel):
    """실제 계약금액 기록 — 정확도 피드백 루프의 입력."""

    actual_cost: int = Field(gt=0)
    contracted_at: datetime


class FeedbackId(BaseModel):
    id: str
