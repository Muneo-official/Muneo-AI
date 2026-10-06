from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, Field, StringConstraints, field_validator, model_validator

from app.schemas.risk import SpaceType

공종_리터럴 = Literal[
    "도배", "장판", "마루", "주방", "욕실", "전기/조명",
    "목공", "도장", "설비", "창호", "도어", "필름", "가구", "철거", "마감/공과잡비",
]


def 공백_제거(v):
    # 프론트가 "1개월 이내"처럼 공백 포함해 보내도 허용
    return v.replace(" ", "") if isinstance(v, str) else v


def 건물연식_정규화(v):
    # 프론트 표기 "10~20년 이하" → "10~20년"
    v = 공백_제거(v)
    return "10~20년" if v == "10~20년이하" else v


def _트럭접근_정규화(v):
    # 프론트 표기 "불가(골목/지하)" → "불가(골목·지하)"
    return v.replace("/", "·") if isinstance(v, str) else v


지역_리터럴 = Literal["서울", "수도권", "지방"]
건물연식_리터럴 = Literal["신축(3년이하)", "10년이하", "10~20년", "20년이상"]
공간유형_리터럴 = SpaceType

# 가견적과 리스크 진단이 같은 집을 다르게 받지 않도록 범위는 여기 한 곳에만 둔다
# (리스크 진단 폼은 app/domain/risk_input_guard.py가 이 값을 가져다 쓴다 — 정한 근거는 그 파일 머리말)
평수_범위 = (5, 150)
방개수_범위 = (1, 10)
층수_범위 = (-2, 70)  # 음수는 지하·반지하. 0층은 없다

# 옵션의 글자 값은 계수표에서 찾거나 검색 문장에 붙는다 — 고르는 값이라 길 이유가 없다
옵션값 = Annotated[str, StringConstraints(max_length=30)]

도배_범위_리터럴 = Literal[
    "전체", "거실", "침실", "주방",
    "침실1", "침실2", "침실3", "침실4", "현관", "벽면",
]


class 도배옵션(BaseModel):
    범위: 도배_범위_리터럴 | list[도배_범위_리터럴] = "전체"
    도배지종류: 옵션값 = "실크벽지"
    초배포함: 옵션값 | None = None


class 마루옵션(BaseModel):
    자재종류: 옵션값 | None = None
    범위: 옵션값 = "전체"
    철거여부: 옵션값 | None = None


class 욕실옵션(BaseModel):
    개수: int = Field(default=1, ge=1, le=5)
    크기: 옵션값 | None = None
    도기교체: 옵션값 | None = None
    방수포함: 옵션값 | None = None
    욕조샤워부스: 옵션값 | None = None
    타일등급: 옵션값 | None = None


class 주방옵션(BaseModel):
    싱크대교체: 옵션값 | None = None
    형태: 옵션값 | None = None
    길이: 옵션값 | None = None


class EstimateRequest(BaseModel):
    """가견적 생성 요청. 필드명은 기존 estimate_engine 입력 규격을 그대로 따른다."""

    공종: list[공종_리터럴] = Field(default_factory=list, max_length=50)
    시공범위: Literal["전체", "부분"] = "부분"
    공간유형: 공간유형_리터럴 = "아파트"
    평수: int = Field(ge=평수_범위[0], le=평수_범위[1])
    방개수: int = Field(default=3, ge=방개수_범위[0], le=방개수_범위[1])
    지역: 지역_리터럴 = "서울"

    건물연식: Annotated[건물연식_리터럴, BeforeValidator(건물연식_정규화)] = "10~20년"
    자재등급: Literal["일반", "중급", "고급"] = "중급"
    철거여부: Literal["있음", "없음", "모름"] = "모름"
    층수: int = Field(default=1, ge=층수_범위[0], le=층수_범위[1])
    엘리베이터: Literal["있음", "없음"] = "있음"
    트럭접근: Annotated[
        Literal["가능", "불가(골목·지하)", "모름"],
        BeforeValidator(_트럭접근_정규화),
    ] = "가능"
    거주중공사: Literal["거주중", "공실"] = "공실"
    공사시기: Annotated[
        Literal["1개월이내", "1~3개월", "3개월이후", "미정"],
        BeforeValidator(공백_제거),
    ] = "미정"

    도배: 도배옵션 | None = None
    마루: 마루옵션 | None = None
    욕실: 욕실옵션 | None = None
    주방: 주방옵션 | None = None

    @field_validator("공종")
    @classmethod
    def _공종_중복_제거(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(v))

    @field_validator("층수")
    @classmethod
    def _0층_없음(cls, v: int) -> int:
        if v == 0:
            raise ValueError("0층은 없습니다. 지하는 음수로 입력해 주세요.")
        return v

    @model_validator(mode="after")
    def _부분_시공은_공종_필수(self):
        # 공종 없이 부분 시공이면 무엇의 값을 낼지 정해지지 않는다 — 엔진까지 보내지 않는다
        if self.시공범위 == "부분" and not self.공종:
            raise ValueError("부분 시공은 공종을 하나 이상 선택해야 합니다.")
        return self


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
    # EstimateRequest로 검증하지 않는다 — 요청 범위를 좁히기 전에 저장된 견적(평수 301, 층수 0 등)이 있어서,
    # 요청 규칙으로 다시 검증하면 그 사용자의 목록 조회가 통째로 실패한다
    input: dict
    result: dict
    status: EstimateStatus
    valid_until: datetime


class FeedbackRequest(BaseModel):
    """실제 계약금액 기록 — 정확도 피드백 루프의 입력."""

    actual_cost: int = Field(gt=0)
    contracted_at: datetime


class FeedbackId(BaseModel):
    id: str
