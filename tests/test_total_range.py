"""
EstimateEngine.generate()의 총 견적 범위 단위테스트 — 중간값은 참고 사례의 중앙값이고, 범위는 그 양쪽으로 같은 폭이다.

실제 사례: 실제 견적서와 비교한 10건 중 7건이 과대였다. 아래 마진과 위 마진을 따로 적용한 범위의 중점을
"중간"으로 내보내서, 위 마진이 더 큰 만큼 중간값이 참고 사례 중앙값보다 항상 높았다(전체 시공·사례 12건
이상에서 +9.5%). 전체 시공은 여기에 마감비 3%까지 곱해졌는데, 사례의 total_cost에는 기타공사가 이미 들어 있다.
"""

import statistics

import pytest

from app.domain.estimate_engine import EstimateEngine, _centered_range


class _Vector:
    def tolist(self):
        return [0.0]


class _Embedder:
    def encode(self, query):
        return _Vector()


class _Repo:
    def __init__(self, cases):
        self._cases = cases

    async def count(self):
        return len(self._cases)

    async def vector_search(self, query_embedding, mongo_filter, limit, num_candidates=150):
        return self._cases

    async def find_by_article_ids(self, article_ids):
        return {}


def _cases(totals: list[int]) -> list[dict]:
    # 도배가 총액의 절반 — 전체 시공의 공종 비중 필터(40%)를 통과한다
    return [{"article_id": str(i), "region": "서울", "size_pyeong": 30, "total_cost": t, "cost_도배": t // 2}
            for i, t in enumerate(totals)]


def _input(시공범위: str, 공종: list[str]) -> dict:
    return {
        "공종": 공종, "시공범위": 시공범위, "공간유형": "아파트", "평수": 30, "방개수": 3, "지역": "서울",
        "건물연식": "10~20년", "자재등급": "중급", "철거여부": "없음", "층수": 1, "엘리베이터": "있음",
        "트럭접근": "가능", "거주중공사": "공실", "공사시기": "미정",
    }


async def _generate(totals: list[int], 시공범위: str, 공종: list[str]) -> dict:
    engine = EstimateEngine(case_repository=_Repo(_cases(totals)), embedder=_Embedder(), reranker=None)
    return await engine.generate(_input(시공범위, 공종))


TOTALS_13 = [20_000_000 + i * 2_000_000 for i in range(13)]  # 중앙값 32,000,000


@pytest.mark.parametrize(("n_cases", "half"), [(12, 0.135), (8, 0.31), (5, 0.33), (4, 0.396)])
def test_centered_range_폭은_기존_마진의_합이고_중간값_양쪽으로_같다(n_cases, half):
    lo, hi = _centered_range(10_000_000, 0.20, 0.46, n_cases)
    assert lo == pytest.approx(10_000_000 * (1 - half), abs=1)
    assert hi == pytest.approx(10_000_000 * (1 + half), abs=1)


async def test_전체_시공_중간값은_참고_사례_총액의_중앙값이다():
    out = await _generate(TOTALS_13, "전체", ["도배"])
    총 = out["총_견적_범위"]
    assert 총["중간"] == statistics.median(TOTALS_13)
    assert 총["중간"] - 총["최소"] == pytest.approx(총["최대"] - 총["중간"], abs=1)
    assert (총["최대"] - 총["최소"]) / 총["중간"] == pytest.approx(0.27, abs=0.001)


async def test_전체_시공은_마감비를_총액에_더하지_않고_총액_안의_몫으로_표시한다():
    out = await _generate(TOTALS_13, "전체", ["도배"])
    총, 마감 = out["총_견적_범위"], out["공종별_단가_범위"]["마감/공과잡비"]
    assert 총["중간"] == 32_000_000  # 3%가 곱해지지 않은 값
    assert 마감["최소"] == int(총["최소"] * 0.03)
    assert 마감["최대"] == int(총["최대"] * 0.03)


async def test_부분_시공_중간값은_공종_중간값의_합이다():
    out = await _generate(TOTALS_13, "부분", ["도배"])
    총 = out["총_견적_범위"]
    assert 총["중간"] == out["공종별_단가_범위"]["도배"]["중간"] == 16_000_000
    assert 총["중간"] - 총["최소"] == pytest.approx(총["최대"] - 총["중간"], abs=1)


async def test_부분_시공은_공종_합에_없는_마감비를_더한다():
    out = await _generate(TOTALS_13, "부분", ["도배", "마감/공과잡비"])
    총 = out["총_견적_범위"]
    assert 총["중간"] == int(16_000_000 * 1.03)
    assert 총["최소"] < 총["중간"] < 총["최대"]
    assert out["공종별_단가_범위"]["마감/공과잡비"]["최대"] > 0


async def test_공종_금액이_없는_부분_시공은_사례_총액의_중앙값을_중간으로_쓴다():
    # 요청 공종의 금액이 사례에 하나도 없으면 사례 총액으로 폴백한다. 이때도 중간은 범위의 중점이 아니다
    totals = [10_000_000, 11_000_000, 12_000_000, 13_000_000, 30_000_000]
    out = await _generate(totals, "부분", ["필름"])
    assert out["총_견적_범위"]["중간"] == 12_000_000
