"""Vision API 구조화 출력(tool use) 정의 — category를 enum으로 강제한다.

처음에는 자유 텍스트 category를 프롬프트 지시로만 유도했다.
"단가참고" 같은 표 제목을 category로 복사하는 문제(pipeline/results/prompt_category_fix.md)를
프롬프트 지시만으로 완전히 막을 수 없었던 이유가 이거다 — 지시를 아무리 정교하게 써도
모델이 자유 텍스트를 낼 수 있는 한 이탈 가능성이 항상 남는다.

여기서는 Anthropic tool use로 category 필드에 JSON schema enum을 걸어, API 차원에서
`pipeline.categories.NORMALIZED_CATEGORIES`(14개) 밖의 값을 낼 수 없게 만든다. 이러면
CATEGORY_NORM을 통한 사후 정규화(normalize_category)가 새로 파싱되는 데이터에는 더 이상
필요 없어진다 — 원본 표기 변형(창호공사/샷시공사/철호공사 등)이라는 문제 자체가 생성 단계에서
사라지기 때문. (기존에 쌓인 데이터를 재검증할 때는 여전히 normalize_category가 필요하다.)
"""

import copy

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

# 리스크 진단(실시간) 전용 — ESTIMATE_TOOL에서 룰이 쓰지 않는 unit·quantity를 뺀다.
# 비용의 약 80%가 출력 토큰이라, 항목마다 반복되는 필드를 줄이면 비용과 호출 시간이 같이 준다
# (실측 요청당 비용 −21~29%, docs/RISK_DETECTOR_COST_LOG.md 5장).
# - code는 남긴다: 청크 중복 제거 키(parsing._chunk_dedup_key)에서 행 식별자 역할을 한다.
#   빼면 대체 키(품명 앞 4글자)가 서로 다른 행을 합쳐 코퍼스 571건 중 243건에서 항목이 사라졌다
# - unit_price는 남긴다: 단가 누락 룰(risk_analyzer)의 유일한 트리거다
# - quantity는 뺀다: 열 뒤바뀜 보정(_fix_column_swap)의 입력이지만, 실측 원본 출력 941개 항목에서 보정이 한 번도
#   필요 없었다(모델이 지시문대로 직접 바로잡음)
# - 크롤링 수집(배치)은 ESTIMATE_TOOL을 그대로 쓴다 — 수집 데이터는 가견적 코퍼스가 되므로 줄이지 않는다
# ESTIMATE_TOOL에서 파생시켜 카테고리 enum·설명이 두 경로에서 어긋나지 않게 한다.
#
# code는 필수로 바꾼다: 필드를 뺀 스키마에서 모델이 한 청크의 code를 통째로 생략했다(S3 청크 하나, 3/3회 0/72개,
# 전체 스키마에선 72/72개). 청크마다 code 유무가 다르면 중복 제거 키가 청크마다 달라져(code 키 ↔ 품명 대체 키)
# 겹침 구간 행이 두 번 남는다. 코드 열이 없는 견적서는 빈 문자열 → 모든 청크가 같은 대체 키를 쓴다.
#
# category 설명에 필름 규칙을 더한다: 필드를 뺀 스키마에서 "필름시공(샷시,문,틀,붙박이,현관 등)"을 3/3회 창호로
# 분류해 필름 가격 이상 이슈가 사라졌다. 가격 체크 코퍼스의 정규화 규칙(시트공사→필름)과 같은 내용이다.
#
# 수전공사 섹션 규칙: 정답 파일의 D1(2026-09-29, "수전공사 섹션 → 설비")에 맞춘 규칙이다. 정규화 표
# (pipeline/categories.py)가 "수전공사"·"수전/위생공사" 섹션을 설비로 보내는 것을 근거로 삼았다.
# 모델은 그 섹션의 욕실 부속(파티션·선반·액세서리·환풍기·인건비)을 항목 내용대로 '욕실'로 분류하곤 했다(축소 전 스키마
# Sonnet 4.6에서 3회 중 2회, Sonnet 5.5에서 13개 중 12개). 처음엔 "수전 항목만"으로 좁게 썼다가 섹션 단위로 넓혔다.
#
# 재검토 필요(공종 정의 통일, #62): 실제 코퍼스(estimate_cases 709건,
# 2026-10-01 집계)는 욕실 부속이 설비·욕실로 거의 반반 섞여 있어(파티션 31:35%, 환풍기 44:44%, 천정재 36:52%) 이 규칙의
# 데이터 근거는 약하다. 리스크 누락 룰(app/domain/risk_analyzer.py)은 수전을 설비, 샤워·환풍기·양변기·세면을 욕실로 본다 —
# 현장 관행과도 맞는 이 기준(설비 = 배관·급배수·수전, 욕실 = 도기·파티션·액세서리·욕실장·천정재·환풍기)으로 바꾸는 것이 유력하다.
#
# 섹션 전체 일반 규칙("항목은 속한 섹션 공종으로")은 쓰지 않는다 — 공종 설명의 "작업 내용 기준"·"폐기물처리→공과잡비"와
# 충돌하고, 정답 파일에 없는 보양·폐기물 항목까지 옮긴다.
# unit은 다시 받는다(2026-10): 수량이 평인지 ㎡인지는 금액 ÷ 단가로 알 수 없어, 수량 과다 판정에 단위가 필요하다.
# quantity는 계속 뺀다 — 금액 ÷ 단가로 계산된다.
RISK_DROPPED_ITEM_FIELDS = ("quantity",)
RISK_FILM_CATEGORY_RULE = "필름·시트지 시공은 붙이는 대상(샷시·문·문틀·붙박이장 등)과 관계없이 '필름'으로 분류한다."
RISK_FAUCET_CATEGORY_RULE = (
    "견적서에 '수전공사' 또는 '수전/위생공사' 구분(섹션)이 있으면, 그 섹션에 속한 항목은 수전뿐 아니라 "
    "샤워 파티션·선반·액세서리·환풍기·천정재·부자재·인건비까지 모두 '설비'로 분류한다."
)
# 도기 규칙: 도기(양변기·세면대)는 욕실 (D4, 2026-10-01). 실제 코퍼스는 양변기·세면기 항목의 다수가 욕실이고(905 vs 약 650),
# 리스크 누락 룰(욕실 필수 항목에 양변기·세면·도기)과 가견적 엔진도 욕실이다. 정규화 표(pipeline/categories.py)의
# "도기공사 → 설비"와는 다르다 — 정규화 표·코퍼스 재집계는 공종 정의 통일 후속 과제에서 맞춘다.
# 규칙이 없으면 Sonnet 5.5가 도기공사 섹션을 5회 중 3회 설비로 보냈다(수전공사 섹션 규칙을 도기까지 넓혀 적용한 것으로 보임).
RISK_TOILET_CATEGORY_RULE = (
    "'도기공사' 구분(섹션)에 속한 항목(양변기·세면대·욕조 등 위생도기와 그 섹션의 방수·젠다이·부자재·인건비)은 "
    "모두 '욕실'로 분류한다."
)
RISK_ESTIMATE_TOOL = copy.deepcopy(ESTIMATE_TOOL)
_risk_item_schema = RISK_ESTIMATE_TOOL["input_schema"]["properties"]["line_items"]["items"]
for _field in RISK_DROPPED_ITEM_FIELDS:
    del _risk_item_schema["properties"][_field]
_risk_item_schema["properties"]["code"]["description"] = (
    "견적서의 코드(항목 번호) 열 값. 코드 열이 있으면 모든 행에 빠짐없이 채우고, 코드 열이 없는 견적서면 빈 문자열."
)
_risk_item_schema["properties"]["category"]["description"] += (
    " " + RISK_FILM_CATEGORY_RULE + " " + RISK_FAUCET_CATEGORY_RULE + " " + RISK_TOILET_CATEGORY_RULE
)
_risk_item_schema["required"] = ["code", *_risk_item_schema["required"]]

# tool use와 함께 쓰는 지시문 — 어떤 표를 읽을지, 집계 행 제외, 금액·단가 열 구분, 잘린 이미지, 자기검증을 담는다.
# 여기에 없는 것: category 표준화(enum이 구조적으로 강제한다), total_cost의 합계 우선순위(위 도구의 total_cost 설명에 있다).
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


# 리스크 진단(실시간) 전용 지시문. 수집용과 다른 점은 "집계 행" 단락 하나다:
#   - 소계 행을 품목과 함께 받는다 — 소계가 품목의 합과 맞는지 검산하려면 견적서에 적힌 소계가 필요하다
#   - 금액이 없는 행("별도", "협의")을 받는다 — 총액에 들어가지 않은 비용이라 소비자에게 가장 위험한 줄인데,
#     수집용 지시문은 금액이 있는 품목만 받아서 리스크 진단이 이 줄을 볼 수 없었다
# 수집용(TOOL_USE_INSTRUCTIONS)은 그대로 둔다. 코퍼스의 품목에 소계 행이 섞이면 공종 금액이 두 번 더해진다.
_AGGREGATE_BLOCK = TOOL_USE_INSTRUCTIONS[TOOL_USE_INSTRUCTIONS.index("━━━ 집계 행은 반드시 제외 ━━━"):
                                         TOOL_USE_INSTRUCTIONS.index("━━━ amount와 unit_price 구분 ━━━")]
RISK_TOOL_USE_INSTRUCTIONS = TOOL_USE_INSTRUCTIONS.replace(_AGGREGATE_BLOCK, """━━━ 소계 행과 금액이 없는 행 ━━━

소계 행은 line_items에 포함한다. 품목과 구분되도록 이렇게 적는다:
  - description은 정확히 "소계"
  - category는 그 소계가 속한 공사 구분의 공종
  - amount는 견적서에 적힌 소계 금액 그대로(품목을 더해서 고치지 않는다), unit_price는 0
  - 소계 행은 견적서에 나온 자리(그 구분의 품목들 앞 또는 뒤)에 그대로 둔다

금액 칸이 비어 있거나 "-", "별도", "협의"로 적힌 행도 품명이 있으면 포함한다. amount와 unit_price는 0으로 적고,
"별도"·"협의" 같은 글자는 description에 그대로 남긴다.

아래 행들은 포함하지 않는다:
  - 견적서 전체의 "합계", "총계", "공사비", "공사비합계", 이윤·보험료·부가세 행
  - 품명 칸이 비어 있고 금액만 있는 행
  - 소제목 행("1) 목공마감재"처럼 금액도 수량도 없는 구분 제목)

""").replace(
    "기록 전 sum(line_items[*].amount)와 total_cost의 차이가", "기록 전 소계 행을 뺀 sum(line_items[*].amount)와 total_cost의 차이가")
