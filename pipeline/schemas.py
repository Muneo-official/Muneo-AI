"""LLM 비전 파싱 결과(pipeline/reference/parse_estimates.py의 출력)를 표현하는 구조 스키마.

parse_estimates.py의 PARSE_PROMPT가 요구하는 JSON 형태를 그대로 따른다. category는
Literal로 못박지 않는다 — 새 원본 표기가 들어올 때마다 스키마 자체가 깨지면 운영이
어려워지므로, "구조는 여기서 검증하고 category가 알려진 값인지는 pipeline/validators.py의
논리 검증에서 다룬다"는 역할 분리를 따른다.
"""

from pydantic import BaseModel, Field


class LineItem(BaseModel):
    code: str = ""
    category: str
    description: str
    unit_price: int = 0
    quantity: float = 1.0
    unit: str = ""
    amount: int = Field(gt=0)  # pipeline/reference/parse_estimates.py 규칙: amount<=0인 항목은 애초에 제외


class ParsedEstimate(BaseModel):
    total_cost: int = Field(gt=0)
    line_items: list[LineItem] = Field(default_factory=list)
