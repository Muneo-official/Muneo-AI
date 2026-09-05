from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SpaceType = Literal["아파트", "빌라", "오피스텔", "단독주택"]
RiskStatus = Literal["정상", "누락", "중복", "불분명"]


@dataclass(frozen=True)
class AnalyzeRiskCommand:
    """Spring 서버에서 전달받은 견적서 분석 요청 값."""

    space_type: str
    pyeong: int
    room_count: int
    floor: int
    elevator: bool
    region: str
    building_age: str
    company_name: str
    image_files: list[bytes]


class SubtitleFields(BaseModel):
    company_name: str
    pyeong: int
    analyzed_date: str


class ConstructionInfo(BaseModel):
    space_type: SpaceType
    room_count: int
    floor: int
    elevator: bool
    region: str
    building_age: str


class RiskCard(BaseModel):
    label: str
    count: int = Field(ge=0)
    description: str


class RiskCards(BaseModel):
    missing: RiskCard
    duplicate: RiskCard
    unclear: RiskCard


class ProcessItem(BaseModel):
    status: RiskStatus
    title: str
    description: str
    guide: str


class ProcessSection(BaseModel):
    process: str
    display_name: str
    items: list[ProcessItem]


class Summary(BaseModel):
    title: str
    total_risk_items: int = Field(ge=0)
    chips: dict[Literal["누락", "중복", "불분명"], int]


class RiskReport(BaseModel):
    title: str
    subtitle_fields: SubtitleFields
    construction_info: ConstructionInfo
    cards: RiskCards
    process_sections: list[ProcessSection]
    summary: Summary


class AnalyzeRiskResponse(BaseModel):
    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "report": {
                    "title": "진단 레포트",
                    "subtitle_fields": {
                        "company_name": "홍길동 인테리어",
                        "pyeong": 30,
                        "analyzed_date": "2026-05-12",
                    },
                    "construction_info": {
                        "space_type": "아파트",
                        "room_count": 3,
                        "floor": 8,
                        "elevator": True,
                        "region": "서울",
                        "building_age": "20년이상",
                    },
                    "cards": {
                        "missing": {
                            "label": "누락항목",
                            "count": 2,
                            "description": "필수 항목 미포함",
                        },
                        "duplicate": {
                            "label": "중복항목",
                            "count": 1,
                            "description": "동일 항목 반복 기재",
                        },
                        "unclear": {
                            "label": "불분명",
                            "count": 3,
                            "description": "모호 표현/정보 불충분",
                        },
                    },
                    "process_sections": [],
                    "summary": {
                        "title": "진단 요약",
                        "total_risk_items": 6,
                        "chips": {"누락": 2, "중복": 1, "불분명": 3},
                    },
                }
            }
        }
    )

    report: RiskReport
