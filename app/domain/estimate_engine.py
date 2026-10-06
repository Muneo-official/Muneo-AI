"""
estimate_engine.py — 사용자 입력(공종·평수·지역 등) → RAG 기반 가견적 생성

숫자 산출 로직에는 LLM을 사용하지 않는다 (실제 사례 통계 기반).
LLM은 이 모듈 밖(자연어 요약, 자유입력 파싱)에서만 사용한다.

기존 종합프로젝트/estimate/estimate_engine.py 로직을 이관.
DB 접근은 이 클래스가 직접 하지 않고 CaseRepository(app/repositories)에 위임한다.
"""

import math
import re
import statistics
from collections import defaultdict

from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer
from starlette.concurrency import run_in_threadpool

from app.core.logging import log_event
from app.repositories.case_repository import CaseRepository
from pipeline.aggregation import DOOR_CATEGORY, DOOR_SOURCE_CATEGORIES, is_door_item
from pipeline.categories import FURNITURE_DOOR_KEYWORDS, normalize_category

# ══════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════

ENGINE_VERSION     = "1.6.0"  # 저장된 견적의 재현성 추적용 (estimates.engine_version)
TOP_K              = 15   # 최종 유사 사례 수 (리랭킹 이후)
RERANK_POOL        = 20   # RRF 결합 이후, cross-encoder에 넣을 후보 수
CASE_TEXT_REQUEST_CAP = 60  # _case_text()의 요청글 트렁케이션 길이. 캡이 넉넉할수록(예: 300)
                            # cross-encoder가 텍스트 길이 자체에 편향돼 순위를 왜곡한다는 게
                            # 확인됨(eval/results/reranker_hybrid_eval.md) — 60이 벡터 단독
                            # 대비 precision@5/10 전부 개선되는 것으로 검증된 값.
SIZE_RANGE         = 6    # 평수 ±6평 필터 — 7→5(a39eaf4)를 거쳐 6으로 조정. 라벨상 relevant 비율이
                           # 평수차 6평 65.5% → 7평 22.2%로 6과 7 사이에서 급락한다.
                           # eval/results/size_range_comparison.md: ±5와 P@15 동일(93.1%, 차이는 노이즈
                           # 범위), 대신 쿼리당 후보가 늘고(중앙값 15→19) 평수+지역+공종 조건 통과가 3건
                           # 미만이라 지역 조건까지 풀리던 쿼리가 없어짐. ±7은 −10.8%p로 확실히 나쁨.
                           # 라벨 규칙(_suggested_relevant)은 순환 방지를 위해 ±5 그대로 둔다.
MAX_SPEC_ITEMS     = 12   # 공종별 명세 최대 항목 수 (ancillary 제외)
SPEC_RATIO         = 0.15 # 비정규화 항목 등장 비율 threshold (전체 사례 수 × 비율)
SCOPE_COVERAGE_MIN = 0.40 # 요청 공종 비용 합계 / 사례 총 비용 최소 비율 (전체 시공용)
FINISHING_COST_KEY = "cost_공과잡비"  # 사례의 마감/공과잡비 금액. 입력 공종 "마감/공과잡비"에 대응한다
CASE_AMOUNT_MIN_VALUES = 3  # 요청 공종 밖의 금액(공과잡비, 철거)을 사례에서 가져와 총액에 더할 때, 그 금액이 있는
                            # 사례가 이 수 이상이어야 한다. 적으면 한 건에 흔들리므로 비율·평당 보정으로 낸다
PARTIAL_SCOPE_POOL = 150  # 부분 시공 요청에서 부분 시공 사례를 고르기 위해 가져오는 후보 수. 부분 시공 사례는
                          # 코퍼스의 약 17%라 기본 후보 풀(40)로는 3건을 채우기 어렵다. $vectorSearch의
                          # limit은 numCandidates(150)를 넘을 수 없다.
FULL_SCOPE_MIN_TRADES = 6  # 도배·바닥·욕실이 모두 있고 공종이 이 수 이상이면 전체 리모델링 사례로 본다
PARTIAL_SCOPE_MIN_CASES = 8  # 부분 시공 사례가 이 수 이상 모일 때만 그 사례로 좁힌다. 3~4건으로 좁히면 한 건에
                             # 크게 흔들리고 범위도 넓어진다. _margin_scale()이 범위를 줄이기 시작하는 건수와 같다.
PARTIAL_SCOPE_MIN_VALUES = 3  # 좁힌 뒤에도 요청 공종마다 금액이 이 수 이상 있어야 한다. 사례가 8건이어도 어떤
                              # 공종의 금액이 1~2건에서만 나오면 그 공종은 한 건에 흔들린다.

# 치수 표기 기호(×/✕/*/+) 정규화: "540*540" → "540×540"
_DIM_SEP_RE = re.compile(r'(\d+)[×✕\*\+](\d+)')

# BM25용 경량 토크나이저 (형태소 분석기 없이 한글 음절/영숫자 단위로만 분리)
_TOKEN_RE = re.compile(r"[\w가-힣]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text or "")


# has_* 플래그 → 사람이 읽는 공종 이름 (build_rag.py의 build_document()와 동일 매핑)
_HAS_TO_WORK = {
    "has_창호": "창호", "has_도배": "도배", "has_타일": "타일",
    "has_가구": "가구", "has_욕실": "욕실", "has_바닥": "바닥",
    "has_전기": "전기", "has_조명": "조명",
}


def line_items_text(case: dict) -> str:
    """request_body_text가 없는 사례를 위한 대체 텍스트 — 실제 시공 품목명으로 구성.

    일부 업체 게시글은 본문에 "고객 의뢰글" 참고 링크가 아예 없어서 request_body_text를
    가져올 데이터 자체가 없다(크롤러 버그 아님 — 업체 템플릿 차이). 이 경우 _case_text()/
    scripts.backfill_new_embeddings.build_document()가 "{평수}평 {지역} {공종} 리모델링"
    같은 헤더만 임베딩하게 되는데, 이런 케이스가 실측 851건 추가 후 재검증에서 24개 쿼리 중
    최대 21개의 벡터 top-20에 무차별하게 걸려 precision을 크게 깎아먹는 게 확인됐다
    (eval/results/reranker_hybrid_eval.md). line_items는 모든 estimate_cases 문서에 항상
    있고 실제로 뭘 시공했는지 구체적으로 담고 있어 이 대체재로 적합하다.
    """
    line_items = (case.get("parsed_estimate") or {}).get("line_items", [])
    seen: set[str] = set()
    parts: list[str] = []
    for item in line_items:
        desc = (item.get("description") or "").strip()
        if desc and desc not in seen:
            seen.add(desc)
            parts.append(desc)
    return " ".join(parts)

# 사용자 지역 → DB region 매핑
REGION_MAP = {
    "서울":  ["서울"],
    "수도권": ["경기", "인천"],
    "지방":  ["부산", "대구", "울산", "광주", "대전", "세종",
               "강원", "충북", "충남", "전북", "전남", "경북", "경남", "제주", "기타"],
}

# 사용자 공종 → DB has_* 플래그
공종_TO_HAS = {
    "도배":       "has_도배",
    "장판":       "has_바닥",
    "마루":       "has_바닥",
    "욕실":       "has_욕실",
    "주방":       "has_가구",
    "가구":       "has_가구",
    "전기/조명":  "has_전기",
    "창호":       "has_창호",
    "도어":       "has_창호",  # has_창호의 키워드에 도어·현관문이 들어 있다. 도어 전용 플래그는 없다
}

# 사용자 공종 → DB cost_* 키
공종_TO_COST = {
    "도배":       ["cost_도배"],
    "장판":       ["cost_바닥"],
    "마루":       ["cost_바닥"],
    "욕실":       ["cost_욕실", "cost_설비", "cost_타일"],
    "주방":       ["cost_가구"],
    "가구":       ["cost_가구"],
    "철거":       ["cost_철거"],
    "전기/조명":  ["cost_전기"],
    "도어":       ["cost_도어"],  # 방문·중문·현관문. 목공·창호 품목에서 품명으로 분리해 집계한다(pipeline/aggregation.py)
    "목공":       ["cost_목공"],
    "도장":       ["cost_도장"],
    "설비":       ["cost_설비"],
    "창호":       ["cost_창호"],
    "필름":       ["cost_필름"],
}

# 사용자 공종 → 파싱 데이터 category 이름 매핑
공종_TO_CATEGORY = {
    "도배":       ["도배공사"],
    "마루":       ["바닥공사"],
    "장판":       ["바닥공사"],
    "욕실":       ["타일공사", "수전공사", "도기공사", "욕실공사"],
    "주방":       ["가구공사"],
    "가구":       ["가구공사"],
    "전기/조명":  ["전기공사", "조명공사"],
    "목공":       ["목공사", "목공"],
    "도장":       ["도장공사"],
    "설비":       ["설비공사", "수전/위생공사"],
    "철거":       ["철거공사"],
    "창호":       ["창호공사"],
    "필름":       ["필름공사"],
    "도어":       [DOOR_CATEGORY],  # 견적서의 분류가 아니라 collect_line_items()가 품명으로 골라 묶는 이름이다
}

공종_EXCLUDE_DESC: dict[str, set] = {
    "마루":  {"장판"},
    "장판":  {"강마루"},
    "주방":  {"붙박이장", "신발장", "수납장", "현관장", "키큰장"},
    "가구":  {"싱크대", "냉장고장", "후드", "주방수전"},
    "욕실":  {"거실바닥타일", "보일러", "난방배관"},  # 설비로 분류된 품목 중 욕실 공사가 아닌 것
}

NORM_MAP = {
    "도배공사": [
        (["LX", "KCC", "자연애", "장판", "강마루", "마루"],  None),
        (["초배", "삼중지"],                "초배지"),
        (["실크"],                          "실크벽지"),
        (["합지"],                          "합지벽지"),
        (["퍼티", "바탕면처리", "벽지제거", "벽 평탄화", "평탄화"], "바탕면처리·퍼티"),
        (["코너비드"],                      "코너비드"),
        (["부직포", "본드", "바인더", "부자재"], "부자재"),
        (["인건비", "안건비", "노무비"],    "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "바닥공사": [
        (["강마루", "강화마루", "원목마루", "마루재", "합판마루", "구정"], "강마루"),
        (["자연애", "장판", "LX", "KCC", "우드름", "우드롬", "우드룸", "사랑애"], "장판"),
        (["합판"],                          "합판"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비"],              "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "타일공사": [
        (["주방벽타일", "주방타일"],         None),
        (["현관"],                          "현관타일"),
        (["발코니"],                        "발코니타일"),
        (["욕실 벽", "욕실벽", "화장실 벽", "화장실벽"], "욕실벽타일"),
        (["욕실 바닥", "욕실바닥", "화장실 바닥", "화장실바닥"], "욕실바닥타일"),
        (["바닥타일"],                      "거실바닥타일"),
        (["코너비트", "코너"],              "코너비트"),
        (["줄눈"],                          "줄눈"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비", "보조공"], "인건비"),
        (["운송비", "양중"],                "운송비"),
    ],
    "수전공사": [
        (["세면기수전", "세면대수전"],       "세면기수전"),
        (["샤워기", "샤워수전", "샤워 수전"], "샤워기"),
        (["슬라이딩장", "슬라이딩바"],      "슬라이딩장"),
        (["욕실거울", "거울"],              "욕실거울"),
        (["SMC"],                           "욕실천정"),
        (["코너선반", "코너 선반"],         "욕실선반"),
        (["환풍기"],                        "욕실환풍기"),
        (["휴지걸이", "수건걸이", "액세서리", "유리코너"], "욕실액세서리"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비"],    "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "도기공사": [
        (["세면도기", "세면기", "반다리"],  "세면기"),
        (["양변도기", "양변기"],            "양변기"),
        (["욕조"],                          "욕조"),
        (["자바라"],                        "자바라트랩"),
        (["SMC"],                           "욕실천정"),
        (["액세서리", "수건걸이", "휴지걸이"], "욕실액세서리"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비", "세팅비"], "인건비"),
        (["운송비", "운반비"],              "운송비"),
    ],
    "가구공사": [
        (["싱크대", "싱크", "사재싱크", "씽크"], "싱크대"),
        (["냉장고장", "냉장고 장"],         "냉장고장"),
        (["붙박이", "붙박이장"],            "붙박이장"),
        (["신발장"],                        "신발장"),
        (["수납장", "다용도실"],            "수납장"),
        (["인조대리석"],                    "인조대리석상판"),
        (["화장대"],                        "화장대"),
        (["수전", "원홀"],                  "주방수전"),
        (["현관장"],                        "현관장"),
        (["키큰장"],                        "키큰장"),
        (["후드"],                          "후드"),
        (["부자재"],                        "부자재"),
        (["인건비", "시공인건비", "안건비", "시공비"], "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "철거공사": [
        (["폐기물", "건축폐기물"],          "폐기물처리"),
        (["사다리차", "장비대"],            "사다리차"),
        (["마루철거", "마루 철거"],         "마루철거"),
        (["타일철거", "타일 철거"],         "타일철거"),
        (["가구철거", "가구 철거"],         "가구철거"),
        (["기타철거"],                      "기타철거"),
        (["부자재", "잡비", "기본재"],      "부자재"),
        (["인건비", "안건비"],              "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "전기공사": [
        (["분전반"],                        "분전반"),
        (["인덕션"],                        "인덕션"),
        (["콘센트"],                        "콘센트"),
        (["스위치"],                        "스위치"),
        (["배선", "배관", "전선", "파이프"], "배선/배관"),
        (["화재감지기", "소방감지기", "감지기"], "감지기"),
        (["차단기"],                        "차단기교체"),
        (["비디오폰"],                      "비디오폰"),
        (["기타소모자재", "기타 소모자재"], "기타소모자재"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비"],    "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "조명공사": [
        (["다운라이트", "매입등"],          "다운라이트"),
        (["간접등", "간접조명"],            "간접조명"),
        (["직부등"],                        "직부등"),
        (["식탁등"],                        "식탁등"),
        (["T5", "T-5"],                     "T5등"),
        (["타공"],                          "타공·배선·조명설치"),
        (["면조명", "엣지"],                "면조명"),
        (["LED"],                           "LED등"),
        (["등기구"],                        "등기구"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비"],    "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "목공사": [
        (["걸레받이"],                      "걸레받이"),
        (["문선"],                          "문선"),
        (["문틀"],                          "문틀"),
        (["천장"],                          "천장목공"),
        (["파티션"],                        "파티션"),
        (["MDF"],                           "MDF"),
        (["합판"],                          "합판"),
        (["석고"],                          "석고보드"),
        (["각재"],                          "각재"),
        (["몰딩"],                          "몰딩"),
        (["장비대"],                        "부자재"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비"],    "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "도장공사": [
        (["탄성", "단성"],                  "탄성코트"),
        (["세라믹"],                        "세라믹코트"),
        (["페인트", "페인", "락카"],        "페인트"),
        (["프라이머"],                      "프라이머"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비", "노무비"],    "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "창호공사": [
        (["현관문"],                        "현관문"),
        (["발코니창", "발코니"],            "발코니창"),
        (["샷시", "새시", "이중창"],        "샷시/새시"),
        (["방화문"],                        "방화문"),
        (["중문"],                          "중문"),
        (["손잡이", "도어체크"],            "문손잡이·경첩"),
        (["ABS", "방문"],                   "방문"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비"],              "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "필름공사": [
        (["문짝", "문 필름"],               "문짝필름"),
        (["문선"],                          "문선필름"),
        (["현관문", "방화문"],              "현관문필름"),
        (["몰딩"],                          "몰딩필름"),
        (["샷시", "새시"],                  "샷시필름"),
        (["시트"],                          "필름자재"),
        (["가구"],                          "가구필름"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비"],              "인건비"),
        (["운송비"],                        "운송비"),
    ],
    "설비공사": [
        (["배관"],                          "배관"),
        (["보일러"],                        "보일러"),
        (["난방"],                          "난방배관"),
        (["부자재"],                        "부자재"),
        (["인건비", "안건비"],              "인건비"),
        (["운송비"],                        "운송비"),
    ],
}

# 품목의 공종이 정규화된 이름("설비", "전기")일 때 쓰는 품명 정리 규칙. NORM_MAP의 키는 견적서 표기("수전공사",
# "조명공사")인데 코퍼스 품목의 공종은 대부분 정규화된 이름이다. 같은 이름으로 정규화되는 키의 규칙을 잇되,
# 인건비·부자재·운송비처럼 어느 품목에나 붙는 낱말의 규칙은 맨 뒤로 보낸다 — 앞 목록의 "인건비" 규칙이 뒤 목록의
# 구체적인 규칙("다운라이트")을 가로채지 않게. 견적서 표기가 남아 있는 품목은 NORM_MAP을 그대로 쓴다.
_GENERIC_SPEC_LABELS = {"인건비", "부자재", "운송비"}
_NORM_RULES: dict[str, list] = {}
for _raw_category, _rules in NORM_MAP.items():
    _NORM_RULES.setdefault(normalize_category(_raw_category) or _raw_category, []).extend(_rules)
for _category, _rules in _NORM_RULES.items():
    _NORM_RULES[_category] = ([r for r in _rules if r[1] not in _GENERIC_SPEC_LABELS]
                              + [r for r in _rules if r[1] in _GENERIC_SPEC_LABELS])

_SKIP_KEYWORDS = ["식대", "주차", "통행료"]

# ── 조정 계수 ──────────────────────────────────────────
# 아래 카테고리는 전부 "시장 가격/비용 정보"라 correction_coefficients 컬렉션에서 버전 관리된다
# (app/repositories/coefficient_repository.py). 여기 있는 값은 그 컬렉션이 비어있을 때만 쓰는
# 폴백/시드 기준값이다.
#
# 반대로 방_마루_비율/도배_범위_비율/방별_침실_비율(방 개수→면적 비율 환산)과 마진 4종
# (전체_LO/HI_MARGIN 등, _margin_scale()과 얽힌 통계적 불확실성 폭)은 "가격"이 아니라
# "도메인을 숫자로 어떻게 모델링할지"에 대한 구조적 가정이라 DB로 안 옮기고 코드에 남긴다 —
# estimate_feedback은 총 계약금액만 들어와서 이런 세부 값 하나하나가 맞는지 검증할 근거가 없고,
# 바꾸려면 TOP_K처럼 재검증(calibration 재확인)이 필요한 성격이기 때문.
DEFAULT_COEFFICIENTS: dict[str, dict | float] = {
    "material_grade": {"일반": 0.85, "중급": 1.0, "고급": 1.25},
    "building_age": {
        "신축(3년이하)": 0.80,
        "10년이하":      0.90,
        "10~20년":       1.00,
        "20년이상":      1.15,
    },
    "region": {"서울": 1.12, "수도권": 1.05, "지방": 1.00},  # 현재 calc_factors에서는 미적용 (RAG 필터가 이미 지역을 반영)
    "occupancy": {"거주중": 1.10, "공실": 1.00},
    "timing": {
        "1개월이내": 1.05,
        "1~3개월":  1.00,
        "3개월이후": 0.95,
        "미정":      1.00,
    },
    "truck_access": {
        "가능":          1.00,
        "불가(골목·지하)": 1.07,
        "모름":          1.03,
    },
    "demolition_cost": {"있음": 25000, "없음": 0, "모름": 12000},  # 원/평
    "lifting_cost_per_floor": 150000,  # 원/층 (엘리베이터 없을 때 사다리차 양중비)
    "finishing_ratio": 0.03,  # 마감/공과잡비 = 총 공사비의 3%
    "wallpaper_type": {"실크벽지": 1.00, "합지벽지": 0.75, "천연벽지": 1.40},
}

# retrieve_cases()가 조건을 풀어 가는 순서 — (평수, 지역, 공종 플래그) 조건을 쓸지. 여기서 3건 이상 못 찾으면
# 마지막으로 조건 없이 찾는다(Stage 4). 평가 스크립트는 이 표를 복사하지 말고 가져다 쓴다.
# 엔진 1.5.0에서 자재등급 단계를 없애 번호가 하나씩 당겨졌다: 예전 Stage 2(등급 완화) → 지금 Stage 1,
# 예전 Stage 3(지역 완화) → 지금 Stage 2. 로그의 stage 값을 볼 때 엔진 버전을 함께 본다.
RETRIEVAL_STAGES: list[tuple[bool, bool, bool]] = [
    (True,  True,  True),   # Stage 1: 평수 + 지역 + 공종
    (True,  False, True),   # Stage 2: 지역 완화
    (False, False, True),   # Stage 3: 평수 완화
]

전체_LO_MARGIN = 0.20
전체_HI_MARGIN = 0.46
부분_LO_MARGIN = 0.22
부분_HI_MARGIN = 0.25


def _margin_scale(n_cases: int) -> tuple[float, float]:
    """(lo_scale, hi_scale) 반환 — 사례 수 기반 양방향 마진 축소."""
    if n_cases >= 12:
        return 0.2, 0.5
    if n_cases >= 8:
        return 0.8, 1.0
    if n_cases >= 5:
        return 1.0, 1.0
    return 1.2, 1.2


def _total_range(mid: int, lo_margin: float, hi_margin: float, n_cases: int) -> tuple[int, int]:
    """총액의 (최소, 최대). 중간값에서 아래로 lo_margin, 위로 hi_margin만큼 — 사례 수에 따라 축소한다.

    범위는 위로 더 넓다. 중간값은 이 범위의 중점이 아니라 호출하는 쪽이 넘긴 값 그대로다.

    위로 넓힌 근거는 총액을 사례의 total_cost로 내던 때의 것이다(이윤·보험료가 빠진 공사비로 저장된 건이 많아
    실제 견적이 참고 사례보다 높게 나오는 쪽이 흔했다). 총액을 공종별 중간값의 합으로 바꾼 뒤(1.6.0)에는 마진을
    다시 정하지 않았다.
    """
    lo_scale, hi_scale = _margin_scale(n_cases)
    return int(mid * (1 - lo_margin * lo_scale)), int(mid * (1 + hi_margin * hi_scale))


도배_범위_비율: dict[str, float] = {
    "전체": 1.00,
    "거실": 0.35,
    "침실": 0.40,
    "주방": 0.10,
    "현관": 0.05,  # 근거 데이터 없음 — 현관 면적 추정값
}
# 벽면만 도배(천장 제외)할 때 곱하는 계수. 벽 ≈ 바닥면적×2.5~3, 천장 ≈ 바닥면적×1 이라는 기하 추정값
# (근거 데이터 없음, calibration 필요)
도배_벽면_계수 = 0.75
방별_침실_비율: dict[int, float] = {1: 0.13, 2: 0.27, 3: 0.40, 4: 0.53}
방_마루_비율: dict[int, float] = {1: 0.70, 2: 0.85, 3: 1.00, 4: 1.15}


# ══════════════════════════════════════════════════════
# EstimateEngine
# ══════════════════════════════════════════════════════

class EstimateEngine:
    """가견적 산출 엔진.

    숫자 산출 로직에는 LLM을 사용하지 않는다 — 실제 사례 통계 기반 산출만 사용.
    벡터 검색·원본 데이터 조회는 CaseRepository에 위임하고, 여기서는
    필터 구성 / 통계 집계 / 보정계수 적용만 담당한다.
    """

    # DB cost_설비 데이터 부재로 지원 불가 — 입력에 포함되어도 무시
    _UNSUPPORTED_공종 = {"설비"}

    def __init__(
        self,
        case_repository: CaseRepository,
        embedder: SentenceTransformer,
        reranker: CrossEncoder | None,  # None이면 cross-encoder 재정렬 없이 RRF 순위를 그대로 쓴다
        vector_candidate_pool: int = 40,
        coefficients: dict | None = None,
        window_includes_door: bool = True,
    ):
        self._cases = case_repository
        self._embedder = embedder
        self._reranker = reranker
        self._vector_candidate_pool = vector_candidate_pool
        self._coefficients = coefficients or {}
        self._coefficient_version = self._coefficients.get("version", "default")
        # True면 "창호"만 고른 요청을 샷시 + 도어로 본다(화면에 "도어" 항목이 생기기 전의 뜻). 요청에 "도어"가
        # 따로 있으면 이 값과 무관하게 "창호"는 샷시만이다.
        self._window_includes_door = window_includes_door

    def _coeff(self, category: str) -> dict | float:
        """버전 관리되는 보정계수 카테고리 조회. DB에 없는 카테고리는 하드코딩 기본값으로 폴백.

        `or` 대신 `in`으로 존재 여부를 확인한다 — finishing_ratio 같은 스칼라 카테고리는
        의도적으로 0으로 튜닝될 수 있는데, falsy 값 기준으로 폴백하면 0이 기본값으로
        조용히 덮어써지는 버그가 생긴다.
        """
        if category in self._coefficients:
            return self._coefficients[category]
        return DEFAULT_COEFFICIENTS[category]

    @staticmethod
    def _normalize_desc(category: str, desc: str) -> tuple:
        for kw in _SKIP_KEYWORDS:
            if kw in desc:
                return None, False
        if category in NORM_MAP:  # 견적서 표기가 남아 있는 품목은 그 공종의 규칙만 쓴다
            rules = NORM_MAP[category]
        else:
            rules = _NORM_RULES.get(normalize_category(category or "") or category, [])
        for keywords, normalized in rules:
            if any(kw in desc for kw in keywords):
                return normalized, (normalized is not None)
        return desc, False

    @staticmethod
    def _normalize_spec_desc(desc: str) -> str:
        return _DIM_SEP_RE.sub(r'\1×\2', desc)

    @staticmethod
    def _case_trades(case: dict) -> set[str]:
        """사례에 금액이 있는 공종. 욕실·타일·설비는 욕실 하나로, 장판·마루는 바닥 하나로 센다."""
        trades = set()
        for key in {k for keys in 공종_TO_COST.values() for k in keys}:
            if int(case.get(key) or 0) > 0:
                name = key[len("cost_"):]
                # 욕실·타일·설비는 욕실로, 도어는 창호로 묶는다 — 도어를 따로 세면 공종 수가 늘어 같은 사례의
                # 전체/부분 판정이 도어를 분리하기 전과 달라진다
                trades.add("욕실" if name in ("욕실", "타일", "설비") else "창호" if name == "도어" else name)
        return trades

    @classmethod
    def _is_partial_case(cls, case: dict) -> bool:
        """전체 리모델링이 아닌 사례인지. 기준은 정답셋의 시공범위 판정과 같다."""
        trades = cls._case_trades(case)
        return not ({"도배", "바닥", "욕실"} <= trades and len(trades) >= FULL_SCOPE_MIN_TRADES)

    @classmethod
    def _is_partial_request(cls, 공종들: list[str]) -> bool:
        """요청한 공종 구성이 전체 리모델링이 아닌지. 사례와 같은 기준으로 본다.

        공종이 없는 요청은 부분 시공 요청이 아니다 — 리스크 진단의 가격 비교가 공종 없이 "부분"으로 호출하는데,
        견적서가 전체 리모델링일 수 있어 부분 시공 사례로 좁히면 안 된다.
        """
        if not 공종들:
            return False
        return cls._is_partial_case({key: 1 for g in 공종들 for key in 공종_TO_COST.get(g, [])})

    def _cost_keys(self, 공종: str, 공종들: list[str]) -> list[str]:
        """이 요청에서 공종의 금액을 읽을 cost_* 키. 키가 여러 개면 사례별로 더한다."""
        keys = list(공종_TO_COST.get(공종, []))
        if 공종 == "욕실" and "설비" in 공종들:
            keys = [k for k in keys if k != "cost_설비"]
        if 공종 == "창호" and self._window_includes_door and "도어" not in 공종들:
            keys.append("cost_도어")
        return keys

    def _value_counts(self, cases: list[dict], 공종들: list[str]) -> dict[str, int]:
        """요청 공종별로, 그 공종의 금액이 있는 사례 수."""
        return {
            g: sum(1 for c in cases if any(int(c.get(k) or 0) > 0 for k in self._cost_keys(g, 공종들)))
            for g in 공종들
        }

    async def _retrieve_partial_cases(self, query: str, query_embedding: list[float], mongo_filter: dict | None,
                                      공종들: list[str], total: int) -> list[dict] | None:
        """부분 시공 사례만으로 고른 참고 사례. 좁힐 수 없으면 None — 호출하는 쪽이 기존 순서대로 찾는다.

        좁히는 조건 (하나라도 어긋나면 좁히지 않는다)
          - 부분 시공 사례가 PARTIAL_SCOPE_MIN_CASES건 이상
          - 최종 사례(TOP_K로 자른 뒤)에서, 후보에 금액이 있던 요청 공종마다 금액이 PARTIAL_SCOPE_MIN_VALUES건
            이상. 리랭킹 전의 후보로 검사하면 자르는 과정에서 그 공종의 금액이 빠질 수 있다.
        """
        pool_n = min(PARTIAL_SCOPE_POOL, total) if total else PARTIAL_SCOPE_POOL
        try:
            pool = await self._cases.vector_search(query_embedding, mongo_filter, pool_n)
            partial = [c for c in pool if self._is_partial_case(c)]
            if len(partial) < PARTIAL_SCOPE_MIN_CASES:
                return None
            reranked = await self._hybrid_rerank(query, partial)
        except Exception as exc:
            log_event("retrieve_cases_stage_error", level="warning", stage="partial", error=str(exc))
            return None

        kept = self._value_counts(reranked, 공종들)
        thin = [g for g, n in self._value_counts(pool, 공종들).items() if n > 0 and kept[g] < PARTIAL_SCOPE_MIN_VALUES]
        if thin:
            log_event("retrieve_cases_partial_skipped", partial_pool_size=len(partial), thin_trades=thin)
            return None
        log_event("retrieve_cases", stage=1, pool_size=len(pool), partial_pool_size=len(partial),
                  case_count=len(reranked), fallback=False, partial_only=True)
        return reranked

    def _filter_by_scope_coverage(self, cases: list[dict], 공종들: list[str],
                                  min_ratio: float = SCOPE_COVERAGE_MIN) -> list[dict]:
        def _coverage(case: dict) -> float:
            tc = int(case.get("total_cost") or 0)
            if tc <= 0:
                return 0.0
            trade_sum = sum(
                int(case.get(k) or 0)
                for g in 공종들
                for k in self._cost_keys(g, 공종들)
            )
            return trade_sum / tc

        filtered = [c for c in cases if _coverage(c) >= min_ratio]
        return filtered if len(filtered) >= 3 else cases

    async def collect_line_items(self, cases, 공종들):
        def spec_categories(공종: str) -> list[str]:
            # 품목의 공종은 "창호공사"처럼 견적서 표기일 수도, "창호"처럼 정규화된 이름일 수도 있다(코퍼스는
            # 대부분 뒤쪽이다). 정규화된 이름으로 맞춘다 — 견적서 표기로만 찾으면 명세가 거의 비어서 나온다.
            cats = list(dict.fromkeys(normalize_category(c) or c for c in 공종_TO_CATEGORY.get(공종, [])))
            if 공종 == "창호" and "cost_도어" in self._cost_keys(공종, 공종들):
                cats.append(DOOR_CATEGORY)  # "창호"가 샷시 + 도어를 뜻하는 요청
            return cats

        target_categories: list[str] = []
        for 공종 in 공종들:
            target_categories.extend(spec_categories(공종))

        if not target_categories:
            return {}

        amounts = defaultdict(lambda: defaultdict(list))

        article_ids = [str(c.get("article_id", "")) for c in cases if c.get("article_id")]
        docs = await self._cases.find_by_article_ids(article_ids)

        for case in cases:
            aid = str(case.get("article_id", ""))
            data = docs.get(aid)
            if not data:
                continue
            pe = data.get("parsed_estimate")
            if not pe:
                continue
            for item in pe.get("line_items", []):
                source_cat = item.get("category") or ""
                # 목공·창호로 분류된 도어 품목은 "도어"로 묶는다. 품명으로 고르는 기준은 금액 집계(cost_도어)와
                # 같다. 창호의 인건비 같은 일반 품목은 집계에서는 도어 몫을 떼지만 여기서는 원래 공종에 둔다.
                norm_cat = normalize_category(source_cat) or source_cat
                is_door = norm_cat in DOOR_SOURCE_CATEGORIES and is_door_item(item.get("description") or "")
                cat = DOOR_CATEGORY if is_door else norm_cat
                # 도어공사에 적힌 가구 문짝(붙박이장 등)은 도어도 창호도 아니다 — 창호 명세에 섞이지 않게 뺀다
                if not is_door and norm_cat == "창호" and any(kw in (item.get("description") or "") for kw in FURNITURE_DOOR_KEYWORDS):
                    continue
                if cat not in target_categories:
                    continue
                amt = int(item.get("amount") or 0)
                if amt <= 0:
                    continue
                desc = self._normalize_spec_desc(item.get("description") or "")
                # 품명 정리 규칙은 도어로 묶기 전의, 품목에 적힌 공종으로 찾는다 — 중문·방문·문틀 규칙이 창호·목공에 있다
                normalized, was_norm = self._normalize_desc(source_cat, desc)
                if normalized is None:
                    continue
                amounts[cat][(normalized, was_norm)].append(amt)

        result = {}
        for 공종 in 공종들:
            cats = spec_categories(공종)
            excluded = 공종_EXCLUDE_DESC.get(공종, set())

            merged: dict[tuple, list] = defaultdict(list)
            for cat in cats:
                for (desc, was_norm), amt_list in amounts.get(cat, {}).items():
                    if desc in excluded:
                        continue
                    merged[(desc, was_norm)].extend(amt_list)

            ratio_min = max(2, math.ceil(len(cases) * SPEC_RATIO))

            items_for_공종: list[dict] = []
            for (desc, was_norm), amt_list in merged.items():
                min_cases = 2 if was_norm else ratio_min
                if len(amt_list) < min_cases:
                    continue
                r = EstimateEngine.cost_range(amt_list, max_ratio=4.0)
                items_for_공종.append({
                    "description": desc,
                    "amount_range": {
                        "최소": r["최소"],
                        "중간": r["중간"],
                        "최대": r["최대"],
                    },
                    "등장_사례_수": len(amt_list),
                })

            ancillary = {"인건비", "운송비", "부자재"}
            items_for_공종.sort(
                key=lambda x: (x["description"] in ancillary, -x["등장_사례_수"])
            )

            non_anc = [x for x in items_for_공종 if x["description"] not in ancillary]
            anc     = [x for x in items_for_공종 if x["description"] in ancillary]
            items_for_공종 = non_anc[:MAX_SPEC_ITEMS] + anc

            if items_for_공종:
                result[공종] = items_for_공종

        return result

    # ── 1. 텍스트 쿼리 생성 ──────────────────────────────

    def build_query(self, inp: dict) -> str:
        parts = [
            f"{inp.get('평수', '?')}평",
            inp.get("지역", ""),
            inp.get("공간유형", "아파트"),
        ]

        if inp.get("시공범위") == "전체":
            parts.append("전체리모델링")
        else:
            parts += inp.get("공종", [])

        if inp.get("건물연식") in ("20년이상", "10~20년"):
            parts.append("구축")
        if inp.get("자재등급") == "고급":
            parts.append("고급자재")

        마루 = inp.get("마루", {})
        if 마루.get("자재종류"):
            parts.append(마루["자재종류"])

        도배 = inp.get("도배", {})
        if 도배.get("도배지종류"):
            parts.append(도배["도배지종류"])

        parts.append("리모델링")
        return " ".join(p for p in parts if p)

    # ── 2. Mongo(Atlas Vector Search) 필터 생성 (점진적 완화) ──

    def _build_filter(self, 평수: int, 지역들: list, 공종들: list,
                      use_size=True, use_region=True, use_has=True):
        conds = []
        if use_size and 평수:
            conds.append({"size_pyeong": {"$gte": 평수 - SIZE_RANGE, "$lte": 평수 + SIZE_RANGE}})
        if use_region and 지역들:
            if len(지역들) == 1:
                conds.append({"region": {"$eq": 지역들[0]}})
            else:
                conds.append({"region": {"$in": 지역들}})
        if use_has:
            seen = set()
            for 공종 in 공종들:
                flag = 공종_TO_HAS.get(공종)
                if flag and flag not in seen:
                    conds.append({flag: {"$eq": "true"}})
                    seen.add(flag)
        if not conds:
            return None
        if len(conds) == 1:
            return conds[0]
        return {"$and": conds}

    @staticmethod
    def _case_text(case: dict) -> str:
        """BM25/cross-encoder 입력용 텍스트. build_rag.py의 build_document()와 동일한 재료로 재구성."""
        size = case.get("size_pyeong", "?")
        region = case.get("region", "")
        works = [name for key, name in _HAS_TO_WORK.items() if case.get(key) == "true"]
        request_text = (case.get("request_body_text") or "").strip() or line_items_text(case)
        header = " ".join(filter(None, [f"{size}평", region, " ".join(works), "리모델링"]))
        if request_text:
            return f"{header}\n{request_text[:CASE_TEXT_REQUEST_CAP]}"
        return header

    @staticmethod
    def _reciprocal_rank_fusion(rank_lists: list[list[str]], k: int = 60) -> dict[str, float]:
        """여러 순위 리스트(각각 id를 유사도 내림차순으로 정렬한 리스트)를 RRF로 결합.

        순수 함수 — 모델 의존 없이 단위테스트 가능.
        반환: {id: rrf_score}, 점수가 높을수록 상위.
        """
        scores: dict[str, float] = defaultdict(float)
        for rank_list in rank_lists:
            for rank, doc_id in enumerate(rank_list, start=1):
                scores[doc_id] += 1.0 / (k + rank)
        return dict(scores)

    async def _hybrid_rerank(self, query: str, cases: list[dict]) -> list[dict]:
        """벡터 검색 후보 풀 → BM25 결합(RRF) → (선택) cross-encoder 재정렬 → 상위 TOP_K.

        후보가 이미 TOP_K 이하면 리랭킹 의미가 없어 그대로 반환한다.
        리랭커가 없으면(settings.use_reranker=False) 하이브리드까지만 수행하고
        RRF 점수 상위 TOP_K를 반환한다 — BM25 결합은 어느 쪽이든 그대로 유지된다.
        """
        if len(cases) <= TOP_K:
            return cases

        ids = [str(c.get("article_id") or i) for i, c in enumerate(cases)]
        texts = [self._case_text(c) for c in cases]
        id_to_case = dict(zip(ids, cases))
        id_to_text = dict(zip(ids, texts))

        # $vectorSearch 결과는 이미 유사도 내림차순으로 정렬되어 있음
        vector_rank_ids = ids

        bm25 = BM25Okapi([_tokenize(t) for t in texts])
        bm25_scores = bm25.get_scores(_tokenize(query))
        bm25_rank_ids = [ids[i] for i in sorted(range(len(ids)), key=lambda i: bm25_scores[i], reverse=True)]

        rrf_scores = self._reciprocal_rank_fusion([vector_rank_ids, bm25_rank_ids])
        pool_ids = sorted(rrf_scores, key=rrf_scores.get, reverse=True)[:min(len(ids), RERANK_POOL)]

        if self._reranker is None:
            reranked = [id_to_case[i] for i in pool_ids]
        else:
            pairs = [(query, id_to_text[i]) for i in pool_ids]
            ce_scores = await run_in_threadpool(self._reranker.predict, pairs)

            order = sorted(range(len(pool_ids)), key=lambda i: ce_scores[i], reverse=True)
            reranked = [id_to_case[pool_ids[i]] for i in order]

        log_event(
            "hybrid_rerank",
            candidate_pool=len(cases),
            rrf_pool=len(pool_ids),
            final=min(len(reranked), TOP_K),
            reranked=self._reranker is not None,
        )
        return reranked[:TOP_K]

    async def retrieve_cases(self, query: str, inp: dict):
        """
        점진적 폴백으로 유사 사례 검색 ($vectorSearch + filter → BM25/RRF → cross-encoder).
          Stage 1: 평수 + 지역 + has_*   (전체 조건)
          Stage 2: 평수 + has_*          (지역 완화)
          Stage 3: has_*만               (평수 완화)
          Stage 4: 필터 없음              (최후 수단)
        자재등급으로는 사례를 거르지 않는다. material_grade가 있는 사례는 코퍼스의 9%뿐이라(700건 중 65건),
        등급으로 거르면 3~5건으로 견적이 나오고 범위가 66~79%까지 넓어졌다. 등급은 calc_factors()의 계수로만
        반영한다 — 등급으로 고른 사례에 계수를 또 곱하면 두 번 반영된다.
        각 Stage에서 벡터 검색으로 후보 풀(vector_candidate_pool)을 넉넉히 가져온 뒤
        하이브리드 리랭킹으로 최종 TOP_K를 추린다.

        부분 시공 요청은 그 전에 같은 지역·평수의 부분 시공 사례만으로 한 번 찾는다. 충분히 모이지 않으면
        위 순서대로(전체 리모델링 사례 포함) 다시 찾는다.
        """
        total = await self._cases.count()
        pool_n = min(self._vector_candidate_pool, total) if total else self._vector_candidate_pool
        평수    = int(inp.get("평수") or 0)
        지역들  = REGION_MAP.get(inp.get("지역", "서울"), ["서울"])
        공종들  = inp.get("공종", [])

        # 임베딩 계산은 CPU-bound 블로킹 연산이므로 이벤트 루프를 막지 않도록 threadpool에서 실행
        query_embedding = (await run_in_threadpool(self._embedder.encode, query)).tolist()

        def stage_filter(flags: tuple[bool, bool, bool]) -> dict | None:
            use_size, use_region, use_has = flags
            return self._build_filter(평수, 지역들, 공종들, use_size=use_size, use_region=use_region, use_has=use_has)

        # 부분 시공 요청은 부분 시공 사례만으로 먼저 찾는다. 같은 공종이라도 부분 시공 사례의 금액은 전체
        # 리모델링 사례보다 훨씬 낮다(코퍼스 평당 중앙값 기준 목공·전기·철거는 약 0.35~0.4배, 창호는 약
        # 0.2배). 전체 리모델링 사례가 섞이면 부분 시공 견적이 2~3배 높게 나왔다.
        산출_공종들 = [g for g in 공종들 if g not in self._UNSUPPORTED_공종 and 공종_TO_COST.get(g)]
        if inp.get("시공범위", "부분") == "부분" and self._is_partial_request(산출_공종들):
            # 같은 지역·평수 조건(Stage 1의 필터)으로 한 번만 찾는다. 지역을 풀면 서울 요청이 지방 가격으로
            # 나온다 — 지역 계수는 "필터가 같은 지역 사례를 가져온다"는 전제로 적용하지 않고 있다.
            partial = await self._retrieve_partial_cases(query, query_embedding, stage_filter(RETRIEVAL_STAGES[0]),
                                                         산출_공종들, total)
            if partial is not None:
                return partial

        for stage, flags in enumerate(RETRIEVAL_STAGES, start=1):
            try:
                cases = await self._cases.vector_search(query_embedding, stage_filter(flags), pool_n)
                if len(cases) >= 3:
                    reranked = await self._hybrid_rerank(query, cases)
                    log_event("retrieve_cases", stage=stage, pool_size=len(cases),
                              case_count=len(reranked), fallback=False)
                    return reranked
            except Exception as exc:
                log_event("retrieve_cases_stage_error", level="warning", stage=stage, error=str(exc))

        cases = await self._cases.vector_search(query_embedding, None, pool_n)
        reranked = await self._hybrid_rerank(query, cases)
        log_event("retrieve_cases", stage=len(RETRIEVAL_STAGES) + 1, pool_size=len(cases),
                  case_count=len(reranked), fallback=True)
        return reranked

    # ── 3. 사례에서 비용 추출 ────────────────────────────

    def extract_costs(self, cases: list[dict], 공종들: list[str]) -> tuple[list[int], dict[str, list[int]]]:
        total_costs: list[int] = []
        cat_costs: dict[str, list[int]] = defaultdict(list)

        for case in cases:
            tc = int(case.get("total_cost") or 0)
            if tc > 0:
                total_costs.append(tc)

            for 공종 in 공종들 + ["철거"]:
                keys = self._cost_keys(공종, 공종들)
                # 키가 여러 개인 공종(욕실)은 사례별 합 하나만 넣는다. 키마다 따로 넣으면 중앙값이
                # 세 부분의 합이 아니라 부분 하나의 크기가 돼, 실제 견적 대비 2배 넘게 낮게 나왔다.
                # 같은 욕실 공사가 사례마다 욕실·설비·타일에 다르게 나뉘어 있어 있는 값만 더한다.
                val = sum(int(case.get(cost_key) or 0) for cost_key in keys)
                if val > 0:
                    cat_costs[공종].append(val)

        return total_costs, cat_costs

    # ── 4. 조정 계수 계산 ────────────────────────────────

    def calc_factors(self, inp: dict):
        factor = 1.0
        notes  = []

        f = self._coeff("material_grade").get(inp.get("자재등급", "중급"), 1.0)
        if f != 1.0:
            factor *= f
            notes.append(f"자재등급 {inp.get('자재등급')} ({f-1:+.0%})")

        f = self._coeff("building_age").get(inp.get("건물연식", "10~20년"), 1.0)
        if f != 1.0:
            factor *= f
            notes.append(f"건물연식 {inp.get('건물연식')} ({f-1:+.0%})")

        f = self._coeff("occupancy").get(inp.get("거주중공사", "공실"), 1.0)
        if f != 1.0:
            factor *= f
            notes.append(f"거주 중 공사 ({f-1:+.0%})")

        f = self._coeff("timing").get(inp.get("공사시기", "미정"), 1.0)
        if f != 1.0:
            factor *= f
            label = "성수기 할증" if f > 1.0 else "비수기 할인"
            notes.append(f"{label} ({f-1:+.1%})")

        # 지역 계수 미적용: RAG 필터가 이미 같은 지역 사례를 가져오므로 이중 적용 방지

        f = self._coeff("truck_access").get(inp.get("트럭접근", "가능"), 1.0)
        if f != 1.0:
            factor *= f
            notes.append(f"트럭 접근 {inp.get('트럭접근')} ({f-1:+.0%})")

        양중 = 0
        if inp.get("엘리베이터") == "없음":
            # 지하(음수)도 그만큼 오르내린다 — 그대로 곱하면 양중비가 음수가 돼 총액을 깎는다
            층수 = abs(int(inp.get("층수") or 1))
            양중 = 층수 * self._coeff("lifting_cost_per_floor")
            notes.append(f"사다리차 양중비 +{양중:,}원 ({층수}층)")

        마감비율_적용 = (
            "마감/공과잡비" in inp.get("공종", []) or
            inp.get("시공범위") == "전체"
        )
        return factor, notes, 양중, 마감비율_적용

    def _demolition_allowance(self, inp: dict) -> int:
        """공종에 철거가 없을 때 총액에 더하는 철거비 보정(평당). 공종에 철거가 있으면 0."""
        if "철거" in inp.get("공종", []):
            return 0
        return self._coeff("demolition_cost").get(inp.get("철거여부", "모름"), 0) * int(inp.get("평수") or 0)

    # ── 5. 공종별 개별 보정계수 계산 ────────────────────────

    def calc_공종_factors(self, inp: dict) -> dict[str, tuple[float, list]]:
        result: dict[str, tuple[float, list]] = {}
        공종들  = inp.get("공종", [])
        방개수  = min(int(inp.get("방개수") or 3), 4)

        if "도배" in 공종들:
            도배_inp = inp.get("도배", {})
            f = 1.0
            notes: list[str] = []

            범위_raw = 도배_inp.get("범위", "전체")
            범위_list = 범위_raw if isinstance(범위_raw, list) else [범위_raw]

            벽면만 = "벽면" in 범위_list
            방들 = [r for r in 범위_list if r != "벽면"]
            # 프론트는 침실을 "침실1"~"침실N"으로 개별 선택하므로 개수로 환산한다
            침실_수 = sum(1 for r in 방들 if r.startswith("침실") and r[2:].isdigit())

            if "전체" in 방들 or (벽면만 and not 방들):
                ratio = 1.0
            else:
                ratio = 0.0
                침실_전체 = 방별_침실_비율.get(방개수, 0.40)
                if "침실" in 방들:
                    ratio += 침실_전체
                elif 침실_수:
                    ratio += min(침실_수 * 침실_전체 / 방개수, 침실_전체)
                for r in 방들:
                    if not r.startswith("침실"):
                        ratio += 도배_범위_비율.get(r, 0.0)
            if 벽면만:
                ratio *= 도배_벽면_계수
            if ratio != 1.0:
                ratio = max(0.05, min(ratio, 1.0))
                f *= ratio
                notes.append(f"도배 범위 {'·'.join(범위_list)} (면적 {ratio:.0%})")

            도배지 = 도배_inp.get("도배지종류", "실크벽지")
            f_지   = self._coeff("wallpaper_type").get(도배지, 1.0)
            if f_지 != 1.0:
                f *= f_지
                notes.append(f"도배지 {도배지} ({f_지-1:+.0%})")

            if f != 1.0:
                result["도배"] = (f, notes)

        for 공종 in ("마루", "장판"):
            if 공종 in 공종들:
                f_마루 = 방_마루_비율.get(방개수, 1.0)
                if f_마루 != 1.0:
                    result[공종] = (
                        f_마루,
                        [f"방 {방개수}개 기준 바닥 면적 보정 ({f_마루-1:+.0%})"],
                    )

        주방_선택 = "주방" in 공종들
        가구_선택 = "가구" in 공종들

        # cost_가구는 case별로 "가구공사" 카테고리 원가 전체를 담는 단일 필드라, 주방(싱크대 등)과
        # 가구(붙박이장 등)가 같은 case 안에 같이 있으면 두 출력 카테고리에 같은 금액이 중복
        # 집계된다 — 둘 다 선택됐을 때만 분배가 필요하다.
        # 60/40(주방/가구) 비율은 검증된 값이 아니다 — Atlas 데이터 확인 결과 실제로 같은 case에
        # 주방+가구가 함께 잡히는 경우는 드물고(3건), 그 3건에서는 오히려 가구 쪽이 더 컸다(~58%).
        # 표본이 3건뿐이라 정밀한 재조정은 보류하고, 방향이 의심스럽다는 것만 남겨둔다.
        # TODO: 더 많은 라벨링 데이터로 실제 분배 비율 재검증 필요.
        if 주방_선택 and 가구_선택:
            result["주방"] = (0.60, ["주방·가구 동시 선택 — 주방(싱크대 등) 60% 배분"])
            result["가구"] = (0.40, ["주방·가구 동시 선택 — 가구(붙박이 등) 40% 배분"])
        # 가구만 선택된 경우엔 분배가 아예 불필요하다 — Atlas 데이터 확인 결과 cost_가구가
        # 있는 case의 71%(46/65)는 주방 항목 없이 순수 가구 비용만 담고 있어서, 이전처럼
        # 무조건 40%로 깎으면 대부분의 케이스에서 이미 맞는 값을 근거 없이 반토막 냈다.

        return result

    # ── 6. 공종별 단가 범위 계산 ─────────────────────────

    @staticmethod
    def cost_range(values, max_ratio: float = None):
        """IQR 기반 범위: 최솟값=P25, 최댓값=P75, 중간=전체 중앙값.

        max_ratio를 주고 최대/최소가 그 비율을 넘으면, 중간값에서 위아래로 같은 비율(√max_ratio)까지만
        남긴다. 비율 안에 있는 범위는 건드리지 않는다. 예전에는 최소를 그대로 두고 최대만 최소 × max_ratio로 깎았는데, P25가 낮으면 깎은 최대가
        중간값보다 작아져 중간값을 최대로 쓰게 됐다 — "최소 90만 / 중간 184만 / 최대 184만"처럼 중간값이
        끝값과 같은 범위가 공종 범위 다섯 개 중 하나꼴로 나왔다.
        """
        if not values:
            return None
        s = sorted(values)
        n = len(s)
        mid = int(statistics.median(s))
        if n < 4:
            lo, hi = s[0], s[-1]
        else:
            lo = s[n // 4]
            hi = s[(3 * n) // 4]
        if max_ratio is not None and mid > 0 and hi > lo * max_ratio:
            side = max_ratio ** 0.5
            lo = max(lo, int(mid / side))
            hi = min(hi, int(mid * side))
        return {"최소": lo, "최대": hi, "중간": mid}

    # ── 7. 최종 가견적 생성 ──────────────────────────────

    async def generate(self, inp: dict) -> dict:
        공종들     = [c for c in inp.get("공종", []) if c not in self._UNSUPPORTED_공종]
        시공범위   = inp.get("시공범위", "부분")
        if 시공범위 == "부분" and not [c for c in 공종들 if c != "마감/공과잡비"]:
            # 지원하지 않는 공종(설비)이나 마감/공과잡비만 고른 부분 시공 — 그대로 가면 값을 낼 공종이 없어
            # 전체 리모델링 사례의 총금액이 견적으로 나간다
            return {"error": "선택한 공종만으로는 견적을 낼 수 없습니다. 다른 공종을 함께 선택해 주세요."}
        query      = self.build_query(inp)
        cases      = await self.retrieve_cases(query, inp)

        추출대상_공종들 = [c for c in 공종들 if c != "마감/공과잡비"]

        if 시공범위 == "전체":
            cases = self._filter_by_scope_coverage(cases, 추출대상_공종들)

        total_costs, cat_costs = self.extract_costs(cases, 추출대상_공종들)

        if not total_costs:
            log_event("generate_no_match", level="warning", query=query, 공종=공종들, 평수=inp.get("평수"))
            return {"error": "유사 사례를 찾을 수 없습니다. 조건을 조정해 주세요."}

        factor, notes, extra, 마감비율_적용 = self.calc_factors(inp)

        공종_factors = self.calc_공종_factors(inp)
        for 공종, (f, 공종_notes) in 공종_factors.items():
            if 공종 in cat_costs:
                cat_costs[공종] = [int(v * f) for v in cat_costs[공종]]
            notes.extend(공종_notes)

        공종별_범위 = {}
        for 공종 in 추출대상_공종들:
            r = self.cost_range(cat_costs.get(공종, []), max_ratio=1.8)
            if r:
                공종별_범위[공종] = {
                    "최소": int(r["최소"] * factor),
                    "중간": int(r["중간"] * factor),
                    "최대": int(r["최대"] * factor),
                }

        # 총액의 "중간"은 공종별 중간값의 합이다. 전체 시공도 사례의 총금액으로 내지 않는다 — 총금액에는 요청에
        # 없는 공종(창호·필름 등)과 확장공사, 이윤·보험료가 들어 있어 요청과 무관하게 총액이 높게 나왔다.
        # 사례마다 요청 공종의 금액을 더한 값의 중앙값도 쓰지 않는다. 그 공종을 하지 않은 사례가 0원으로 들어가,
        # 참고 사례 절반이 샷시를 하지 않은 요청에서 총액이 공종별 금액의 합보다 크게 낮았다.
        # 범위는 위로 더 넓어서 중점으로 계산하지 않는다 — 중점을 쓰면 중간값이 항상 높게 나온다.
        마감_범위 = None
        마감_비율 = self._coeff("finishing_ratio")
        공종_합_총액 = bool(공종별_범위)
        if 공종_합_총액:
            # 철거를 요청하지 않았으면 공종 합에 철거공사가 없으므로 철거비를 더한다. 전체 시공에서 철거가 있다고
            # 했으면 참고 사례의 철거 금액을 쓴다 — 평당 보정액은 전체 리모델링의 철거비(코퍼스 중앙값 평당 약
            # 7.8만 원)의 3분의 1이다. 부분 시공에는 전체 리모델링 사례의 철거 금액이 너무 커서 보정액을 쓴다
            철거_금액 = cat_costs.get("철거", [])
            if (시공범위 == "전체" and "철거" not in 공종들 and inp.get("철거여부") == "있음"
                    and len(철거_금액) >= CASE_AMOUNT_MIN_VALUES):
                철거_보정 = int(self.cost_range(철거_금액)["중간"] * factor)
                notes.append(f"철거비 +{철거_보정:,}원 (참고 사례의 철거 금액)")
            else:
                철거_보정 = self._demolition_allowance(inp)
                if 철거_보정:
                    notes.append(f"철거비 보정 +{철거_보정:,}원")
            # 장판과 마루는 둘 다 사례의 바닥 금액을 읽는다. 함께 고르면 총액에는 한 번만 넣는다
            바닥_중복 = {"장판", "마루"} <= 공종별_범위.keys()
            if 바닥_중복:
                notes.append("장판·마루는 같은 바닥 금액이라 총액에 한 번만 포함")
            adj_mid = sum(r["중간"] for g, r in 공종별_범위.items() if not (바닥_중복 and g == "마루")) + extra + 철거_보정
            if 시공범위 == "전체":
                # 전체 시공의 마감/공과잡비는 다른 공종처럼 사례의 금액으로 낸다. 금액이 있는 사례가 적으면 한 건에
                # 흔들리므로 비율로 더한다. 비율이 0이면(마감을 내지 않는 설정) 사례의 금액도 쓰지 않는다
                마감_금액 = [v for v in (int(c.get(FINISHING_COST_KEY) or 0) for c in cases) if v > 0]
                if 마감_비율 and len(마감_금액) >= CASE_AMOUNT_MIN_VALUES:
                    r = self.cost_range(마감_금액, max_ratio=1.8)
                    마감_범위 = {k: int(v * factor) for k, v in r.items()}
                    adj_mid += 마감_범위["중간"]
                    notes.append("마감/공과잡비는 참고 사례의 금액으로 총액에 포함")
                adj_lo, adj_hi = _total_range(adj_mid, 전체_LO_MARGIN, 전체_HI_MARGIN, len(cases))
            else:
                adj_lo, adj_hi = _total_range(adj_mid, 부분_LO_MARGIN, 부분_HI_MARGIN, len(cases))
        elif 시공범위 == "전체":
            # 요청 공종의 금액을 가진 사례가 없을 때만 사례의 총금액으로 낸다
            adj_mid = int(self.cost_range(total_costs)["중간"] * factor) + extra
            adj_lo, adj_hi = _total_range(adj_mid, 전체_LO_MARGIN, 전체_HI_MARGIN, len(cases))
        else:
            r = self.cost_range(total_costs)
            adj_lo = int(r["최소"] * factor) + extra
            adj_mid = int(r["중간"] * factor) + extra
            adj_hi = int(r["최대"] * factor) + extra

        # 마감/공과잡비를 사례의 금액으로 내지 못했으면 비율로 낸다. 공종 중간값의 합에는 마감이 없으므로 더하고,
        # 사례의 총금액으로 구한 총액에는 견적서의 기타공사가 대부분 들어 있어 총액 안의 몫으로만 표시한다.
        if 마감비율_적용 and 마감_범위 is None:
            마감_범위 = {
                "최소": int(adj_lo * 마감_비율),
                "중간": int(adj_mid * 마감_비율),
                "최대": int(adj_hi * 마감_비율),
            }
            if 공종_합_총액:
                adj_lo = int(adj_lo * (1 + 마감_비율))
                adj_mid = int(adj_mid * (1 + 마감_비율))
                adj_hi = int(adj_hi * (1 + 마감_비율))
                notes.append(f"마감/공과잡비 포함 (총 공사비의 {마감_비율:.0%})")
            else:
                notes.append(f"마감/공과잡비는 총액에 포함된 금액 중 약 {마감_비율:.0%}로 표시")

        def _cost_per_pyeong(c: dict) -> int:
            # cost_per_pyeong은 ingest 시점에 미리 계산해 저장한 필드라, size_pyeong이
            # 이후 별도로 보정된 레코드(약 709건 중 116건, 2026-09-17 확인)는 total_cost/
            # size_pyeong이 멀쩡한데도 이 필드만 0으로 남아있었다. 저장값을 신뢰하지 않고
            # 매번 total_cost/size_pyeong으로 다시 계산해 이 불일치를 원천 차단한다.
            size = c.get("size_pyeong") or 0
            total = c.get("total_cost") or 0
            return int(total / size) if size > 0 else 0

        참고_사례 = sorted(
            [
                {
                    "article_id": c.get("article_id"),
                    "지역":   c.get("region"),
                    "평수":   c.get("size_pyeong"),
                    "총금액": int(c.get("total_cost") or 0),
                    "평당":   _cost_per_pyeong(c),
                }
                for c in cases
                if c.get("total_cost")
            ],
            key=lambda x: abs(x["평수"] - inp.get("평수", 0))
        )[:5]

        공종별_항목_명세 = await self.collect_line_items(cases, 추출대상_공종들)

        if 마감_범위 and 마감_범위["최대"] > 0:
            공종별_범위["마감/공과잡비"] = 마감_범위
            평수 = int(inp.get("평수") or 30)
            철거포함 = "철거" in 공종들
            엘베있음 = inp.get("엘리베이터") != "없음"

            마감_spec = [
                {"description": "현장보양",
                 "amount_range": {"최소": 평수 * 3_000, "중간": 평수 * 5_000, "최대": 평수 * 7_000},
                 "등장_사례_수": None},
                {"description": "입주청소",
                 "amount_range": {"최소": 평수 * 7_000, "중간": 평수 * 10_000, "최대": 평수 * 13_000},
                 "등장_사례_수": None},
                {"description": "실리콘마감",
                 "amount_range": {"최소": 80_000, "중간": 115_000, "최대": 150_000},
                 "등장_사례_수": None},
            ]
            if 엘베있음:
                마감_spec.insert(1, {
                    "description": "엘리베이터보양",
                    "amount_range": {"최소": 80_000, "중간": 115_000, "최대": 150_000},
                    "등장_사례_수": None,
                })
            if not 철거포함:
                마감_spec.append({
                    "description": "폐기물처리",
                    "amount_range": {"최소": 200_000, "중간": 350_000, "최대": 500_000},
                    "등장_사례_수": None,
                })
            공종별_항목_명세["마감/공과잡비"] = 마감_spec

        출력_공종들 = list(공종들)
        if 마감비율_적용 and "마감/공과잡비" not in 출력_공종들:
            출력_공종들.append("마감/공과잡비")

        데이터_부족_공종 = [
            g for g in 추출대상_공종들
            if g not in 공종별_범위
        ]
        if 데이터_부족_공종 and 공종_합_총액:
            notes.append(f"{'·'.join(데이터_부족_공종)} 금액은 참고 사례에 없어 총액에 포함되지 않음")

        reference_case_ids = sorted({
            str(c.get("article_id")) for c in cases if c.get("article_id")
        })

        output = {
            "총_견적_범위": {
                "최소": adj_lo,
                "최대": adj_hi,
                "중간": adj_mid,
            },
            "공종별_단가_범위": 공종별_범위,
            "공종별_항목_명세": 공종별_항목_명세,
            "보정_적용":    notes,
            "시공범위":     시공범위,
            "선택_공종":    출력_공종들,
            "참고_사례_수": len(total_costs),
            "참고_사례":    참고_사례,
            "검색_쿼리":    query,
            "engine_version": ENGINE_VERSION,
            "coefficient_version": self._coefficient_version,
            "reference_case_ids": reference_case_ids,
        }

        if 데이터_부족_공종:
            output["데이터_부족_공종"] = 데이터_부족_공종

        log_event(
            "generate_success",
            query=query,
            참고_사례_수=len(total_costs),
            시공범위=시공범위,
            선택_공종=출력_공종들,
        )

        실공종_수 = len([c for c in 공종들 if c != "마감/공과잡비"])
        if 실공종_수 == 1:
            if all(self._is_partial_case(c) for c in cases):
                output["단독시공_주의"] = "단일 공종 요청입니다. 부분 시공 사례에서 해당 공종 비용을 추출하여 산출했습니다."
            else:
                output["단독시공_주의"] = (
                    "단일 공종 요청입니다. 전체 리모델링 사례에서 해당 공종 비용을 추출하여 산출했으며, "
                    "단독 시공 시 실제 가격이 5~10% 높을 수 있습니다."
                )

        return output
