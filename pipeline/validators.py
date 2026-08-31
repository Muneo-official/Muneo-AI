"""파싱 결과 검증 — 스키마 통과 후의 논리 검증 규칙과 신뢰도 스코어링.

각 규칙은 예외를 던지지 않고 ValidationIssue 목록을 반환한다 — 이 모듈의 용도가
"하나라도 틀리면 거부"가 아니라 "얼마나 문제가 있는지 진단·집계"이기 때문이다
(eval/pricing_gap_diagnostic.py와 같은 진단 스크립트 스타일을 따름).
"""

from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError

from pipeline.categories import FURNITURE_DOOR_KEYWORDS, normalize_category
from pipeline.schemas import ParsedEstimate

SIZE_PYEONG_MIN = 5
SIZE_PYEONG_MAX = 200

# pipeline/reference/parse_estimates.py의 _add_consistency_warning()과 동일한 임계값 —
# 파싱 단계가 이미 이 기준으로 _parse_warning을 남기므로, 검증 단계도 같은 기준을 써서
# "파싱 시점 경고"와 "사후 재검증 결과"가 어긋나지 않게 한다.
TOTAL_CONSISTENCY_TOLERANCE = 0.20

Severity = Literal["error", "warning"]


@dataclass
class ValidationIssue:
    rule: str
    severity: Severity
    message: str


@dataclass
class ValidationResult:
    issues: list[ValidationIssue] = field(default_factory=list)
    reclassification_suggestions: list[dict] = field(default_factory=list)
    confidence: float = 1.0

    @property
    def has_errors(self) -> bool:
        return any(i.severity == "error" for i in self.issues)


def validate_size_pyeong(size_pyeong: int) -> list[ValidationIssue]:
    """crawler.py의 `re.search(r"(\\d+)\\s*평", body_text)`가 본문 전체에서 첫 매치를
    그대로 신뢰하는 구조라, article_id 같은 무관한 숫자가 잘못 잡히는 사례가 실제로
    관측됐다(docs/IMPLEMENTATION_LOG.md 2-4, 13건). 범위 밖 값을 여기서 걸러낸다."""
    if not (SIZE_PYEONG_MIN <= size_pyeong <= SIZE_PYEONG_MAX):
        return [ValidationIssue(
            rule="size_pyeong_range",
            severity="error",
            message=f"size_pyeong={size_pyeong}이(가) 정상 범위({SIZE_PYEONG_MIN}~{SIZE_PYEONG_MAX})를 벗어남 "
                     f"— 크롤링 단계의 평수 추출 정규식이 무관한 숫자를 잡았을 가능성",
        )]
    return []


def validate_total_consistency(parsed: ParsedEstimate) -> list[ValidationIssue]:
    line_sum = sum(item.amount for item in parsed.line_items)
    if parsed.total_cost <= 0:
        return []
    error_rate = abs(line_sum - parsed.total_cost) / parsed.total_cost
    if error_rate > TOTAL_CONSISTENCY_TOLERANCE:
        return [ValidationIssue(
            rule="total_consistency",
            severity="warning",
            message=f"line_items 합계({line_sum:,})와 total_cost({parsed.total_cost:,})의 오차가 "
                     f"{error_rate:.1%}로 허용치({TOTAL_CONSISTENCY_TOLERANCE:.0%})를 초과함",
        )]
    return []


UNKNOWN_CATEGORY_ERROR_RATIO = 0.5   # 미인식 category가 total_cost의 이 비율을 넘으면 error
UNKNOWN_CATEGORY_WARNING_RATIO = 0.2  # 이 비율을 넘으면 warning, 그 이하는 이슈로 안 봄


def validate_known_categories(parsed: ParsedEstimate) -> list[ValidationIssue]:
    """CATEGORY_NORM(+ normalize_category의 쉼표 분해)으로도 인식 못 하는 category는
    build_category_costs()에서 조용히 버려진다(pipeline/reference/build_rag.py:184).

    처음엔 미인식 category 개수만큼 이슈를 냈는데("기타공사"/"단가참고" 등 4개만 섞여도
    -0.4), 실제 데이터에 돌려보니 신뢰도가 낮게 나온 사례의 77%가 순수히 이 개수 페널티
    때문이었다(전체 스키마/평수 오류는 별도로 14건뿐) — 케이스 하나에 사소한 미분류
    항목이 여러 개 있다고 해서 그 케이스 전체가 못 미더운 건 아니다. **개수가 아니라
    total_cost 대비 금액 비중**으로 판단하도록 고쳤다: 참고 사례로서의 신뢰도는
    "이름 모를 카테고리가 몇 종류 있냐"가 아니라 "그게 견적 전체에서 얼마나 큰
    비중이냐"로 결정돼야 한다.
    """
    if parsed.total_cost <= 0:
        return []

    unknown_amount = sum(item.amount for item in parsed.line_items if normalize_category(item.category) is None)
    ratio = unknown_amount / parsed.total_cost
    if ratio <= UNKNOWN_CATEGORY_WARNING_RATIO:
        return []

    unknown_cats = sorted({item.category for item in parsed.line_items if normalize_category(item.category) is None})
    severity: Severity = "error" if ratio > UNKNOWN_CATEGORY_ERROR_RATIO else "warning"
    return [ValidationIssue(
        rule="unknown_category",
        severity=severity,
        message=f"미인식 category가 total_cost의 {ratio:.0%}를 차지함 ({', '.join(unknown_cats)}) "
                 "— cost_* 집계에서 조용히 누락됨",
    )]


def suggest_door_reclassification(parsed: ParsedEstimate) -> list[dict]:
    """"도어공사"로 분류된 항목 중 가구 문짝으로 의심되는 것을 재분류 후보로 제안한다.

    build_rag.py의 CATEGORY_NORM은 "도어공사"를 전부 "창호"로 매핑하는데, 파싱 프롬프트의
    표준화 규칙(ABS도어·문공사·목문틀 → 도어공사)이 현관문뿐 아니라 붙박이장·신발장 문짝까지
    같은 카테고리로 묶어버린다. eval/results/pricing_gap_diagnostic.md에서 실제 사례(890396)로
    확인된 패턴 — 코드를 고치기 전에 먼저 얼마나 흔한지 집계하기 위한 제안 단계다.
    """
    suggestions = []
    for idx, item in enumerate(parsed.line_items):
        if normalize_category(item.category) != "창호":
            continue
        if any(kw in item.description for kw in FURNITURE_DOOR_KEYWORDS):
            suggestions.append({
                "line_item_index": idx,
                "description": item.description,
                "amount": item.amount,
                "current_category": item.category,
                "suggested_category": "가구공사",
            })
    return suggestions


def _compute_confidence(issues: list[ValidationIssue]) -> float:
    score = 1.0
    for issue in issues:
        score -= 0.3 if issue.severity == "error" else 0.1
    return max(0.0, score)


def validate_case(raw_parsed_estimate: dict, size_pyeong: int) -> ValidationResult:
    """estimate_cases 문서 하나의 parsed_estimate + size_pyeong을 검증한다."""
    issues: list[ValidationIssue] = []

    try:
        parsed = ParsedEstimate.model_validate(raw_parsed_estimate)
    except ValidationError as e:
        issues.append(ValidationIssue(
            rule="schema",
            severity="error",
            message=f"parsed_estimate 스키마 검증 실패: {e.error_count()}건 — {e.errors()[0]['msg']}",
        ))
        return ValidationResult(issues=issues, confidence=_compute_confidence(issues))

    issues += validate_size_pyeong(size_pyeong)
    issues += validate_total_consistency(parsed)
    issues += validate_known_categories(parsed)
    reclass = suggest_door_reclassification(parsed)

    return ValidationResult(
        issues=issues,
        reclassification_suggestions=reclass,
        confidence=_compute_confidence(issues),
    )
