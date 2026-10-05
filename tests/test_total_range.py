"""
EstimateEngine.generate()의 총 견적 범위 단위테스트 — 중간값은 범위의 중점이 아니다.

실제 사례: 실제 견적서와 비교한 10건 중 7건이 과대였다. 아래 마진과 위 마진을 따로 적용한 범위의 중점을
"중간"으로 내보내서, 위 마진이 더 큰 만큼 중간값이 참고 사례 중앙값보다 항상 높았다(전체 시공·사례 12건
이상에서 +9.5%). 전체 시공은 여기에 마감비 3%까지 곱해졌는데, 사례의 total_cost에는 기타공사가 대부분 들어 있다.
범위(아래 마진, 위 마진)는 그대로 두고 중간값만 바로잡았다. 1.6.0부터 전체 시공의 중간값은 사례 총금액의
중앙값이 아니라 공종별 중간값의 합이다(tests/test_full_scope_total.py).
"""

import statistics

import pytest

from app.domain.estimate_engine import EstimateEngine, _total_range


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


@pytest.mark.parametrize(("n_cases", "lo", "hi"), [(12, 0.96, 1.23), (8, 0.84, 1.46), (5, 0.80, 1.46), (4, 0.76, 1.552)])
def test_total_range_아래_위_마진을_사례_수에_따라_줄인다(n_cases, lo, hi):
    got_lo, got_hi = _total_range(10_000_000, 0.20, 0.46, n_cases)
    assert got_lo == pytest.approx(10_000_000 * lo, abs=1)
    assert got_hi == pytest.approx(10_000_000 * hi, abs=1)


async def test_전체_시공_중간값은_공종_중간값의_합이다():
    out = await _generate(TOTALS_13, "전체", ["도배"])
    총 = out["총_견적_범위"]
    # 사례의 총금액(중앙값 3,200만)이 아니라 요청한 도배의 중간값 1,600만 + 마감 3%
    assert 총["중간"] == int(16_000_000 * 1.03)
    # 범위는 위로 더 넓다 — 중간값은 범위의 중점이 아니다
    assert 총["최소"] == pytest.approx(16_000_000 * 0.96 * 1.03, abs=2)
    assert 총["최대"] == pytest.approx(16_000_000 * 1.23 * 1.03, abs=2)
    assert 총["중간"] < (총["최소"] + 총["최대"]) // 2


async def test_전체_시공은_사례에_공과잡비_금액이_없으면_마감비를_비율로_더한다():
    out = await _generate(TOTALS_13, "전체", ["도배"])
    assert out["공종별_단가_범위"]["마감/공과잡비"]["중간"] == int(16_000_000 * 0.03)
    assert "마감/공과잡비 포함 (총 공사비의 3%)" in out["보정_적용"]


async def test_부분_시공_중간값은_공종_중간값의_합이다():
    out = await _generate(TOTALS_13, "부분", ["도배"])
    총 = out["총_견적_범위"]
    assert 총["중간"] == out["공종별_단가_범위"]["도배"]["중간"] == 16_000_000
    assert 총["최소"] == pytest.approx(16_000_000 * (1 - 0.22 * 0.2), abs=1)
    assert 총["최대"] == pytest.approx(16_000_000 * (1 + 0.25 * 0.5), abs=1)


async def test_부분_시공은_공종_합에_없는_마감비를_더한다():
    out = await _generate(TOTALS_13, "부분", ["도배", "마감/공과잡비"])
    총 = out["총_견적_범위"]
    assert 총["중간"] == int(16_000_000 * 1.03)
    assert 총["최소"] < 총["중간"] < 총["최대"]
    # 마감 항목의 중간은 마감을 더하기 전 총액 중간값의 3% — 마감 범위의 중점이 아니다
    assert out["공종별_단가_범위"]["마감/공과잡비"]["중간"] == int(16_000_000 * 0.03)
    assert "마감/공과잡비 포함 (총 공사비의 3%)" in out["보정_적용"]


async def test_공종_금액이_없는_부분_시공은_사례_총액의_중앙값을_중간으로_쓴다():
    # 요청 공종의 금액이 사례에 하나도 없으면 사례 총액으로 폴백한다. 이때도 중간은 범위의 중점이 아니다
    totals = [10_000_000, 11_000_000, 12_000_000, 13_000_000, 30_000_000]
    out = await _generate(totals, "부분", ["필름", "마감/공과잡비"])
    assert out["총_견적_범위"]["중간"] == 12_000_000
    # 사례 총액에서 구한 총액이라 마감을 더하지 않고, 마감 항목의 중간도 총액 중간값의 3%다
    assert out["공종별_단가_범위"]["마감/공과잡비"]["중간"] == int(12_000_000 * 0.03)
