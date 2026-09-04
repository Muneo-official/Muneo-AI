"""
eval/pricing_gap_diagnostic.py — 공종별 가격 예측이 실제 사례값과 얼마나 벌어지는지 대량 진단.

배경: fix/wallpaper-range-silent-fallback, fix/partial-scope-cost-underestimation에서
발견한 버그 3개(도배 범위 검증, IQR 클램프 중앙값 왜곡, 가구 40% 컷)는 전부 실제 견적서
2장을 손으로 스팟체크하다 잡아낸 것이다. n=2로는 "이게 우연인지 패턴인지" 확정할 수
없다는 게 반복해서 나온 문제였고, 특히 finishing_ratio(마감/공과잡비 3%)와 필름 공종의
저평가는 n=2로는 손댈 근거가 부족해서 보류했었다.

이 스크립트는 estimate_cases 코퍼스 자체를 ground truth로 써서 표본을 크게 늘린다:
각 사례가 실제로 가진 cost_* 필드값(그 사례의 실제 공사비)을, 그 사례의 다른 속성
(평수/지역/자재등급 등)으로 EstimateEngine.generate()를 돌렸을 때 나오는 예측값과
비교한다 — 일종의 self-consistency 백테스트.

한계(정직하게 남겨둔다):
  - 시공범위/건물연식/방개수 등 case 코퍼스에 없는 필드는 추측값(기본값)을 쓴다.
    이게 특히 도배 범위(항상 "전체"로 가정)처럼 실제와 다를 수 있는 값에서 노이즈를 만든다.
  - 사례 자신이 검색 후보 풀에 포함될 수 있다(self-match). 상위 10~20건 중 하나일 뿐이라
    중앙값/IQR을 크게 왜곡하진 않을 것으로 보지만, 완전히 배제하진 않았다.
  - 욕실은 cost_욕실+cost_설비+cost_타일 세 필드가 합산되는 구조라 단일 필드 대 단일 필드
    비교가 애매해서 이번 진단에서는 제외했다.

그래서 이 결과도 "확정"이 아니라 "n=2보다는 훨씬 믿을 만한 다음 근거"로 취급해야 한다.
"""

import argparse
import asyncio
import random
import statistics
import sys
from collections import defaultdict

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine
from app.repositories.case_repository import CaseRepository
from app.repositories.coefficient_repository import CoefficientRepository

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

load_dotenv()

# cost_* 필드 -> 그 필드 하나로 단순 비교 가능한 공종. (욕실 등 다중 필드 합산 카테고리는 제외)
COST_FIELD_TO_공종 = {
    "cost_도배": "도배",
    "cost_바닥": "마루",
    "cost_가구": "가구",
    "cost_전기": "전기/조명",
    "cost_목공": "목공",
    "cost_도장": "도장",
    "cost_창호": "창호",
    "cost_필름": "필름",
}


async def main(sample_size: int, seed: int) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]

    case_repository = CaseRepository(collection=db["estimate_cases"], settings=settings)
    coefficient_repository = CoefficientRepository(collection=db["correction_coefficients"])
    coefficients = await coefficient_repository.get_active()

    from sentence_transformers import SentenceTransformer

    embedder = SentenceTransformer(settings.embed_model)
    # 리랭커는 뺀다 — 진단 목적상 매칭 순위 품질이 아니라 통계적 가격 산출만 보면 되고,
    # 수백 건 돌리는데 cross-encoder까지 태우면 너무 느려진다.
    engine = EstimateEngine(
        case_repository=case_repository, embedder=embedder, reranker=None, coefficients=coefficients,
    )

    all_cases = await db["estimate_cases"].find({}, {"embedding": 0}).to_list(length=None)
    random.seed(seed)
    sample = random.sample(all_cases, min(sample_size, len(all_cases)))

    ratios: dict[str, list[float]] = defaultdict(list)
    skipped_no_공종 = 0
    skipped_no_match = 0
    skipped_error = 0

    for i, case in enumerate(sample, 1):
        공종들 = [name for field, name in COST_FIELD_TO_공종.items() if (case.get(field) or 0) > 0]
        if not 공종들:
            skipped_no_공종 += 1
            continue

        input_data = {
            "공종": 공종들,
            "시공범위": "부분",
            "평수": case.get("size_pyeong") or 20,
            "방개수": 3,
            "지역": case.get("region") or "서울",
            "건물연식": "10~20년",
            "자재등급": case.get("material_grade") or "중급",
            "철거여부": "모름",
        }
        if "도배" in 공종들:
            input_data["도배"] = {"범위": "전체"}
        if "마루" in 공종들:
            input_data["마루"] = {"범위": "전체"}

        try:
            result = await engine.generate(input_data)
        except Exception:
            skipped_error += 1
            continue

        if "error" in result:
            skipped_no_match += 1
            continue

        공종별_예측 = result.get("공종별_단가_범위", {})
        for field, name in COST_FIELD_TO_공종.items():
            if name not in 공종들:
                continue
            actual = case.get(field)
            pred = 공종별_예측.get(name, {}).get("중간")
            if not actual or not pred:
                continue
            ratios[name].append(pred / actual)

        if i % 20 == 0:
            print(f"  ...{i}/{len(sample)}건 처리", file=sys.stderr)

    client.close()

    print(f"\n샘플 {len(sample)}건 (공종 없음 스킵 {skipped_no_공종}, 매칭 실패 스킵 {skipped_no_match}, "
          f"에러 스킵 {skipped_error})\n")
    print(f"{'공종':<10}{'n':>6}{'중앙값(예측/실제)':>20}{'평균 오차율(MAPE)':>20}")
    print("-" * 56)
    for name in sorted(ratios, key=lambda k: -len(ratios[k])):
        rs = ratios[name]
        if len(rs) < 5:
            continue
        median_ratio = statistics.median(rs)
        mape = statistics.mean(abs(r - 1) for r in rs)
        flag = "  <-- 저평가 의심" if median_ratio < 0.85 else ("  <-- 고평가 의심" if median_ratio > 1.15 else "")
        print(f"{name:<10}{len(rs):>6}{median_ratio:>18.2f}{mape:>19.1%}{flag}")

    print("\n(중앙값 1.00 = 예측이 실제와 정확히 일치. 0.70이면 실제의 70%로 저평가한다는 뜻)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=150, help="샘플링할 사례 수 (기본 150)")
    parser.add_argument("--seed", type=int, default=42, help="랜덤 시드 (기본 42)")
    args = parser.parse_args()
    asyncio.run(main(args.n, args.seed))
