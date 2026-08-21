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

# ══════════════════════════════════════════════════════
# 설정
# ══════════════════════════════════════════════════════

ENGINE_VERSION     = "1.0.0"  # 저장된 견적의 재현성 추적용 (estimates.engine_version)
TOP_K              = 15   # 최종 유사 사례 수 (리랭킹 이후)
RERANK_POOL        = 20   # RRF 결합 이후, cross-encoder에 넣을 후보 수
SIZE_RANGE         = 7    # 평수 ±7평 필터
MAX_SPEC_ITEMS     = 12   # 공종별 명세 최대 항목 수 (ancillary 제외)
SPEC_RATIO         = 0.15 # 비정규화 항목 등장 비율 threshold (전체 사례 수 × 비율)
SCOPE_COVERAGE_MIN = 0.40 # 요청 공종 비용 합계 / 사례 총 비용 최소 비율 (전체 시공용)

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
    "욕실":       ["타일공사", "수전공사", "도기공사"],
    "주방":       ["가구공사"],
    "가구":       ["가구공사"],
    "전기/조명":  ["전기공사", "조명공사"],
    "목공":       ["목공사", "목공"],
    "도장":       ["도장공사"],
    "설비":       ["설비공사", "수전/위생공사"],
    "철거":       ["철거공사"],
    "창호":       ["창호공사"],
    "필름":       ["필름공사"],
}

공종_EXCLUDE_DESC: dict[str, set] = {
    "마루":  {"장판"},
    "장판":  {"강마루"},
    "주방":  {"붙박이장", "신발장", "수납장", "현관장", "키큰장"},
    "가구":  {"싱크대", "냉장고장", "후드", "주방수전"},
    "욕실":  {"거실바닥타일"},
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

자재등급_TO_GRADE = {"일반": "일반", "중급": "중급", "고급": "고급"}

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


도배_범위_비율: dict[str, float] = {
    "전체": 1.00,
    "거실": 0.35,
    "침실": 0.40,
    "주방": 0.10,
}
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
        reranker: CrossEncoder,
        vector_candidate_pool: int = 40,
        coefficients: dict | None = None,
    ):
        self._cases = case_repository
        self._embedder = embedder
        self._reranker = reranker
        self._vector_candidate_pool = vector_candidate_pool
        self._coefficients = coefficients or {}
        self._coefficient_version = self._coefficients.get("version", "default")

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
        rules = NORM_MAP.get(category, [])
        for keywords, normalized in rules:
            if any(kw in desc for kw in keywords):
                return normalized, (normalized is not None)
        return desc, False

    @staticmethod
    def _normalize_spec_desc(desc: str) -> str:
        return _DIM_SEP_RE.sub(r'\1×\2', desc)

    @staticmethod
    def _filter_by_scope_coverage(cases: list[dict], 공종들: list[str],
                                   min_ratio: float = SCOPE_COVERAGE_MIN) -> list[dict]:
        def _coverage(case: dict) -> float:
            tc = int(case.get("total_cost") or 0)
            if tc <= 0:
                return 0.0
            trade_sum = sum(
                int(case.get(k) or 0)
                for g in 공종들
                for k in 공종_TO_COST.get(g, [])
            )
            return trade_sum / tc

        filtered = [c for c in cases if _coverage(c) >= min_ratio]
        return filtered if len(filtered) >= 3 else cases

    async def collect_line_items(self, cases, 공종들):
        target_categories: list[str] = []
        for 공종 in 공종들:
            target_categories.extend(공종_TO_CATEGORY.get(공종, []))

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
                cat = item.get("category", "")
                if cat not in target_categories:
                    continue
                amt = int(item.get("amount") or 0)
                if amt <= 0:
                    continue
                desc = self._normalize_spec_desc(item.get("description", ""))
                normalized, was_norm = self._normalize_desc(cat, desc)
                if normalized is None:
                    continue
                amounts[cat][(normalized, was_norm)].append(amt)

        result = {}
        for 공종 in 공종들:
            cats = 공종_TO_CATEGORY.get(공종, [])
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
                      use_size=True, use_region=True, use_has=True,
                      use_grade=False, grade: str = None):
        conds = []
        if use_size and 평수:
            conds.append({"size_pyeong": {"$gte": 평수 - SIZE_RANGE, "$lte": 평수 + SIZE_RANGE}})
        if use_region and 지역들:
            if len(지역들) == 1:
                conds.append({"region": {"$eq": 지역들[0]}})
            else:
                conds.append({"region": {"$in": 지역들}})
        if use_grade and grade:
            conds.append({"material_grade": {"$eq": grade}})
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
        request_text = (case.get("request_body_text") or "").strip()
        header = " ".join(filter(None, [f"{size}평", region, " ".join(works), "리모델링"]))
        if request_text:
            return f"{header}\n{request_text[:300]}"
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
        """벡터 검색 후보 풀 → BM25 결합(RRF) → cross-encoder 재정렬 → 상위 TOP_K.

        후보가 이미 TOP_K 이하면 리랭킹 의미가 없어 그대로 반환한다.
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

        pairs = [(query, id_to_text[i]) for i in pool_ids]
        ce_scores = await run_in_threadpool(self._reranker.predict, pairs)

        order = sorted(range(len(pool_ids)), key=lambda i: ce_scores[i], reverse=True)
        reranked = [id_to_case[pool_ids[i]] for i in order]

        log_event(
            "hybrid_rerank",
            candidate_pool=len(cases),
            rrf_pool=len(pool_ids),
            final=min(len(reranked), TOP_K),
        )
        return reranked[:TOP_K]

    async def retrieve_cases(self, query: str, inp: dict):
        """
        점진적 폴백으로 유사 사례 검색 ($vectorSearch + filter → BM25/RRF → cross-encoder).
          Stage 1: 평수 + 지역 + has_* + 자재등급  (전체 조건)
          Stage 2: 평수 + 지역 + has_*             (등급 완화)
          Stage 3: 평수 + has_*                    (지역 완화)
          Stage 4: has_*만                         (평수 완화)
          Stage 5: 필터 없음                        (최후 수단)
        각 Stage에서 벡터 검색으로 후보 풀(vector_candidate_pool)을 넉넉히 가져온 뒤
        하이브리드 리랭킹으로 최종 TOP_K를 추린다.
        """
        total = await self._cases.count()
        pool_n = min(self._vector_candidate_pool, total) if total else self._vector_candidate_pool
        평수    = int(inp.get("평수") or 0)
        지역들  = REGION_MAP.get(inp.get("지역", "서울"), ["서울"])
        공종들  = inp.get("공종", [])
        grade   = 자재등급_TO_GRADE.get(inp.get("자재등급", "중급"), "중급")

        # 임베딩 계산은 CPU-bound 블로킹 연산이므로 이벤트 루프를 막지 않도록 threadpool에서 실행
        query_embedding = (await run_in_threadpool(self._embedder.encode, query)).tolist()

        for stage, (use_size, use_region, use_has, use_grade) in enumerate([
            (True,  True,  True,  True),   # Stage 1: 전체 조건 + 등급
            (True,  True,  True,  False),  # Stage 2: 등급 완화
            (True,  False, True,  False),  # Stage 3: 지역 완화
            (False, False, True,  False),  # Stage 4: 평수 완화
            (False, False, False, False),  # Stage 5: 필터 없음
        ], start=1):
            mongo_filter = self._build_filter(평수, 지역들, 공종들,
                                       use_size=use_size,
                                       use_region=use_region,
                                       use_has=use_has,
                                       use_grade=use_grade,
                                       grade=grade)
            try:
                cases = await self._cases.vector_search(query_embedding, mongo_filter, pool_n)
                if len(cases) >= 3:
                    reranked = await self._hybrid_rerank(query, cases)
                    log_event("retrieve_cases", stage=stage, pool_size=len(cases),
                              case_count=len(reranked), fallback=False)
                    return reranked
            except Exception as exc:
                log_event("retrieve_cases_stage_error", level="warning", stage=stage, error=str(exc))

        cases = await self._cases.vector_search(query_embedding, None, pool_n)
        reranked = await self._hybrid_rerank(query, cases)
        log_event("retrieve_cases", stage=5, pool_size=len(cases), case_count=len(reranked), fallback=True)
        return reranked

    # ── 3. 사례에서 비용 추출 ────────────────────────────

    def extract_costs(self, cases: list[dict], 공종들: list[str]) -> tuple[list[int], dict[str, list[int]]]:
        total_costs: list[int] = []
        cat_costs: dict[str, list[int]] = defaultdict(list)

        욕실_keys = list(공종_TO_COST.get("욕실", []))
        if "욕실" in 공종들 and "설비" in 공종들:
            욕실_keys = [k for k in 욕실_keys if k != "cost_설비"]

        for case in cases:
            tc = int(case.get("total_cost") or 0)
            if tc > 0:
                total_costs.append(tc)

            for 공종 in 공종들 + ["철거"]:
                keys = 욕실_keys if 공종 == "욕실" else 공종_TO_COST.get(공종, [])
                for cost_key in keys:
                    val = int(case.get(cost_key) or 0)
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
            층수 = int(inp.get("층수") or 1)
            양중 = 층수 * self._coeff("lifting_cost_per_floor")
            notes.append(f"사다리차 양중비 +{양중:,}원 ({층수}층)")

        평수 = int(inp.get("평수") or 0)
        if "철거" not in inp.get("공종", []):
            철거추가 = self._coeff("demolition_cost").get(inp.get("철거여부", "모름"), 0) * 평수
            if 철거추가:
                notes.append(f"철거비 보정 +{철거추가:,}원")
        else:
            철거추가 = 0

        마감비율_적용 = (
            "마감/공과잡비" in inp.get("공종", []) or
            inp.get("시공범위") == "전체"
        )
        if 마감비율_적용:
            notes.append(f"마감/공과잡비 포함 (총 공사비의 {self._coeff('finishing_ratio'):.0%})")

        return factor, notes, 양중 + 철거추가, 마감비율_적용

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

            if "전체" not in 범위_list:
                ratio = 0.0
                for r in 범위_list:
                    if r == "침실":
                        ratio += 방별_침실_비율.get(방개수, 0.40)
                    else:
                        ratio += 도배_범위_비율.get(r, 0.0)
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

        if 주방_선택 and 가구_선택:
            result["주방"] = (0.60, ["주방·가구 동시 선택 — 주방(싱크대 등) 60% 배분"])
            result["가구"] = (0.40, ["주방·가구 동시 선택 — 가구(붙박이 등) 40% 배분"])
        elif 가구_선택:
            result["가구"] = (0.40, ["가구(붙박이·신발장) 단독 선택 (~40% 적용)"])

        return result

    # ── 6. 공종별 단가 범위 계산 ─────────────────────────

    @staticmethod
    def cost_range(values, max_ratio: float = None):
        """IQR 기반 범위: 최솟값=P25, 최댓값=P75, 중간=전체 중앙값.
        max_ratio 지정 시 P75/P25 비율을 해당 값으로 클램프."""
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
        if max_ratio is not None and lo > 0 and hi > lo * max_ratio:
            hi = int(lo * max_ratio)
        mid = max(lo, min(mid, hi))
        return {"최소": lo, "최대": hi, "중간": mid}

    # ── 7. 최종 가견적 생성 ──────────────────────────────

    async def generate(self, inp: dict) -> dict:
        공종들     = [c for c in inp.get("공종", []) if c not in self._UNSUPPORTED_공종]
        시공범위   = inp.get("시공범위", "부분")
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

        마감_lo = 마감_hi = 0
        마감_비율 = self._coeff("finishing_ratio")

        ms_lo, ms_hi = _margin_scale(len(cases))
        if inp.get("시공범위") == "전체" and total_costs:
            r = self.cost_range(total_costs)
            adj_mid_raw = int(r["중간"] * factor) + extra
            adj_lo = int(adj_mid_raw * (1 - 전체_LO_MARGIN * ms_lo))
            adj_hi = int(adj_mid_raw * (1 + 전체_HI_MARGIN * ms_hi))
            if 마감비율_적용:
                마감_lo = int(adj_lo * 마감_비율)
                마감_hi = int(adj_hi * 마감_비율)
                adj_lo = int(adj_lo * (1 + 마감_비율))
                adj_hi = int(adj_hi * (1 + 마감_비율))
        elif 공종별_범위:
            adj_mid_raw = sum(r["중간"] for r in 공종별_범위.values()) + extra
            adj_lo = int(adj_mid_raw * (1 - 부분_LO_MARGIN * ms_lo))
            adj_hi = int(adj_mid_raw * (1 + 부분_HI_MARGIN * ms_hi))
            if 마감비율_적용:
                마감_lo = int(adj_lo * 마감_비율)
                마감_hi = int(adj_hi * 마감_비율)
                adj_lo = int(adj_lo * (1 + 마감_비율))
                adj_hi = int(adj_hi * (1 + 마감_비율))
        else:
            r = self.cost_range(total_costs)
            adj_lo = int(r["최소"] * factor) + extra
            adj_hi = int(r["최대"] * factor) + extra
            if 마감비율_적용:
                마감_lo = int(adj_lo * 마감_비율)
                마감_hi = int(adj_hi * 마감_비율)
                adj_lo = int(adj_lo * (1 + 마감_비율))
                adj_hi = int(adj_hi * (1 + 마감_비율))

        adj_mid = (adj_lo + adj_hi) // 2

        참고_사례 = sorted(
            [
                {
                    "article_id": c.get("article_id"),
                    "지역":   c.get("region"),
                    "평수":   c.get("size_pyeong"),
                    "총금액": int(c.get("total_cost") or 0),
                    "평당":   int(c.get("cost_per_pyeong") or 0),
                }
                for c in cases
                if c.get("total_cost")
            ],
            key=lambda x: abs(x["평수"] - inp.get("평수", 0))
        )[:5]

        공종별_항목_명세 = await self.collect_line_items(cases, 추출대상_공종들)

        if 마감비율_적용 and 마감_hi > 0:
            공종별_범위["마감/공과잡비"] = {
                "최소": 마감_lo,
                "중간": (마감_lo + 마감_hi) // 2,
                "최대": 마감_hi,
            }
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
            output["단독시공_주의"] = (
                "단일 공종 요청입니다. 전체 리모델링 사례에서 해당 공종 비용을 추출하여 산출했으며, "
                "단독 시공 시 실제 가격이 5~10% 높을 수 있습니다."
            )

        return output
