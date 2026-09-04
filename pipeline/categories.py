"""파싱된 line_item의 category 원본 표기 → 정규화 매핑.

pipeline/reference/build_rag.py의 CATEGORY_NORM을 그대로 가져왔다 — 이 딕셔너리가
실제 이미지에서 관찰된 표기 변형(OCR 오인식 포함)을 담고 있는 유일한 소스라 새로
추측해서 만들지 않았다.
"""

CATEGORY_NORM: dict[str, str] = {
    "철거공사": "철거", "철거": "철거",
    "착공공사": "철거",                         # 착공 단계 철거 포함
    "목공사": "목공", "목공": "목공",
    "도배공사": "도배", "도배": "도배",
    "바닥공사": "바닥", "바닥": "바닥", "마루공사": "바닥",
    "바닥장공사": "바닥",                        # OCR 변형 (바닥장→바닥)
    "마루": "바닥",
    "타일공사": "타일", "타일": "타일",
    "욕실공사": "욕실", "설비공사": "설비", "수전/위생공사": "설비",
    "수전공사": "설비",                          # "수전/위생공사"의 슬래시 없는 변형 (scripts/validate_existing_cases.py로 3,628건 발견)
    "도기공사": "설비",                          # pipeline/reference/parse_estimates.py 규칙7이 표준으로 지정하는데 원본 매핑에 누락돼 있었음
    "도기,수전": "설비",                         # 복합 표기, 의미상 동일 버킷
    "전기공사": "전기", "전기": "전기", "조명공사": "전기",
    "가구공사": "가구", "주방가구공사": "가구", "싱크공사": "가구",
    "창호공사": "창호", "샷시공사": "창호",
    "현호공사": "창호",                          # OCR 오인식 (현호→창호)
    "철호공사": "창호",                          # OCR 오인식 (철호→창호)
    "도어공사": "창호",                          # 현관문·방문 도어 포함 — 가구 문짝과 혼입 위험, validators.py 참고
    "샷시": "창호", "새시": "창호", "새시공사": "창호",
    "필름공사": "필름", "시트공사": "필름",      # 필름·시트는 동일 공종
    "도장공사": "도장",
    "폐기물처리": "철거",
    "기타/공과잡비": "공과잡비",                 # pipeline/prompts.py 규칙7에서 신설한 표준 카테고리
    "확장공사": "확장",                          # 기존 12개 공종 어디에도 안 맞아 별도 버킷으로 분리
}

NORMALIZED_CATEGORIES: tuple[str, ...] = tuple(sorted(set(CATEGORY_NORM.values())))


def normalize_category(raw: str) -> str | None:
    """CATEGORY_NORM 직접 조회 실패 시, 쉼표로 결합된 복합 표기("목공,도어",
    "철거,설비공사" 등)를 분해해 첫 번째로 인식되는 토큰을 사용한다.

    실제 데이터에서 이런 복합 표기가 2,530건의 미인식 category 중 상당수를 차지했다
    (scripts/validate_existing_cases.py 결과) — 조합을 하나하나 하드코딩하는 대신
    일반 규칙으로 처리해 앞으로 나올 새 조합에도 대응한다.

    입력이 이미 정규화된 값(NORMALIZED_CATEGORIES)이면 그대로 반환한다 — pipeline/tool_schema.py
    의 tool use는 CATEGORY_NORM의 원본 표기가 아니라 이미 정규화된 값을 직접 출력하므로
    (예: "가구공사"가 아니라 "가구"), 이 경우를 놓치면 정상 데이터가 unknown_category로
    잘못 잡힌다(pipeline/results/vision_api_integration.md에서 실제로 발견된 버그).
    """
    if raw in NORMALIZED_CATEGORIES:
        return raw
    if raw in CATEGORY_NORM:
        return CATEGORY_NORM[raw]
    if "," in raw:
        for token in raw.split(","):
            token = token.strip()
            if token in CATEGORY_NORM:
                return CATEGORY_NORM[token]
    return None


# "도어공사"로 분류됐지만 실제로는 가구(붙박이장·신발장 등) 문짝인 경우를 구분하는 키워드.
# app/domain/estimate_engine.py의 공종_EXCLUDE_DESC["주방"]와 동일한 어휘를 재사용한다 —
# 이 프로젝트에서 "가구 문짝"을 지칭할 때 이미 쓰고 있는 표준 키워드 집합이라 새로 만들지 않았다.
FURNITURE_DOOR_KEYWORDS: frozenset[str] = frozenset({
    "붙박이장", "신발장", "수납장", "현관장", "키큰장",
})
