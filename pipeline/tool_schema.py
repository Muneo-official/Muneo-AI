"""Vision API 구조화 출력(tool use) 정의 — category를 enum으로 강제한다.

Phase 1(pipeline/prompts.py)까지는 자유 텍스트 category를 프롬프트 지시로만 유도했다.
"단가참고" 같은 표 제목을 category로 복사하는 문제(pipeline/results/prompt_category_fix.md)를
프롬프트 지시만으로 완전히 막을 수 없었던 이유가 이거다 — 지시를 아무리 정교하게 써도
모델이 자유 텍스트를 낼 수 있는 한 이탈 가능성이 항상 남는다.

여기서는 Anthropic tool use로 category 필드에 JSON schema enum을 걸어, API 차원에서
`pipeline.categories.NORMALIZED_CATEGORIES`(14개) 밖의 값을 낼 수 없게 만든다. 이러면
CATEGORY_NORM을 통한 사후 정규화(normalize_category)가 새로 파싱되는 데이터에는 더 이상
필요 없어진다 — 원본 표기 변형(창호공사/샷시공사/철호공사 등)이라는 문제 자체가 생성 단계에서
사라지기 때문. (기존에 쌓인 데이터를 재검증할 때는 여전히 normalize_category가 필요하다.)
"""

from pipeline.categories import NORMALIZED_CATEGORIES

TOOL_NAME = "record_estimate"

ESTIMATE_TOOL = {
    "name": TOOL_NAME,
    "description": (
        "인테리어 공사 견적서 이미지에서 추출한 구조화 데이터를 기록한다. "
        "이미지가 견적서가 아니면(평면도, 현장사진, 로고, 배너 등) is_estimate=false만 채우고 "
        "total_cost/line_items는 생략한다."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "is_estimate": {"type": "boolean"},
            "total_cost": {
                "type": "integer",
                "description": "부가세 제외 공사비 합계. 우선순위: 합계/공사비합계 행 > "
                                "부가세포함합계/1.1 > 표에서 가장 큰 합계 행.",
            },
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "code": {"type": "string"},
                        "category": {
                            "type": "string",
                            "enum": list(NORMALIZED_CATEGORIES),
                            "description": (
                                "이 항목이 속하는 공종. 반드시 이 목록 중 하나로 분류한다 — "
                                "이미지에 다른 표현(예: '단가참고', '샷시공사')이 쓰여 있어도 "
                                "실제 작업 내용을 보고 이 14개 중 가장 가까운 것으로 매핑한다. "
                                "승강기 보양/주민동의서/폐기물처리/하자보수/보증증권/준공청소 "
                                "같은 부대비용은 '공과잡비'로 분류한다."
                            ),
                        },
                        "description": {"type": "string"},
                        "unit_price": {"type": "integer"},
                        "quantity": {"type": "number"},
                        "unit": {"type": "string"},
                        "amount": {"type": "integer"},
                    },
                    "required": ["category", "description", "amount"],
                },
            },
        },
        "required": ["is_estimate"],
    },
}

# tool use와 함께 쓰는 지시문 — 표/집계행 판별, total_cost 산정, 열 뒤바뀜 수정 등은
# pipeline/prompts.py의 규칙 1~6과 동일하되, category 표준화(규칙 7/7-1)는 enum이
# 구조적으로 강제하므로 프롬프트에서 뺐다.
TOOL_USE_INSTRUCTIONS = """이 이미지가 인테리어 공사 견적서인지 판단하고, record_estimate 도구를 호출해 결과를 기록해줘.

━━━ 어떤 테이블을 파싱할 것인가 ━━━

견적서에는 두 종류의 테이블이 있을 수 있다:
  [요약] 공사 구분별 합계만 나열 (수량·단가 없음, 행 10~20개)
  [상세] 개별 품목마다 수량·단가·금액 있음 (행 30개 이상)

상세 테이블이 보이면 반드시 상세 테이블만 파싱한다. 요약 테이블만 있을 때만 요약 테이블을
파싱한다.

━━━ 집계 행은 반드시 제외 ━━━

아래 행들은 line_items에 절대 포함하지 않는다:
  - description이 "소계", "합계", "총계", "공사비합계", "계", "공사합계"인 행
  - description이 카테고리명과 동일한 행
  - 품명 칸이 비어 있고 금액만 있는 행
  - 상세 테이블 내 카테고리 구분 소계 행

포함하는 행: 구체적인 품명이 있는 개별 항목만. 판별 기준은 description에 구체적인
재료명·작업명이 있으면 포함, 공사 분류명만 있으면 제외.

━━━ amount와 unit_price 구분 ━━━

열 순서: 코드 | 품명 | 규격 | 수량 | 단위 | 단가 | 금액
  amount = 금액 열 (단가 × 수량, 행 전체 청구금액)
  unit_price = 단가 열 (단위당 가격, 항상 amount 이하)

unit_price > amount이고 amount × quantity ≈ unit_price이면 열이 뒤바뀐 것으로 보고
unit_price와 amount를 교환한다.

━━━ 이미지가 잘린 경우 ━━━

하단이 잘려 마지막 행이 불완전하면 완전히 보이는 행만 파싱한다. total_cost는 이미지에
명시된 합계 값만 사용한다(추정 금지).

━━━ 자기검증 ━━━

기록 전 sum(line_items[*].amount)와 total_cost의 차이가 10% 이상이면 이미지를 다시
검토해 누락된 행을 찾는다. 재검토 후에도 차이가 나면 이미지에 명시된 합계 값을 그대로
total_cost로 쓴다(sum으로 덮어쓰지 않는다).
"""
