"""
부분 시공 요청의 참고 사례 선택 단위테스트 — 부분 시공 사례만으로 먼저 찾고, 모자라면 전체 사례로 돌아간다.

실제 사례: 실제 견적서와 비교한 부분 시공 4건 중 3건이 과대였다(공사비 기준 +49%, +75%, +209%). 참고 사례
대부분이 전체 리모델링이었고, 같은 공종이라도 전체 리모델링 사례의 금액은 부분 시공 사례보다 훨씬 높다
(코퍼스 평당 중앙값 기준 목공·전기·철거는 부분 시공이 약 0.35~0.4배).
"""

from app.domain.estimate_engine import PARTIAL_SCOPE_MIN_CASES, PARTIAL_SCOPE_POOL, EstimateEngine


class _Vector:
    def tolist(self):
        return [0.0]


class _Embedder:
    def encode(self, query):
        return _Vector()


class _Repo:
    def __init__(self, cases):
        self._cases = cases
        self.limits = []

    async def count(self):
        return 700

    async def vector_search(self, query_embedding, mongo_filter, limit, num_candidates=150):
        self.limits.append(limit)
        return self._cases[:limit]

    async def find_by_article_ids(self, article_ids):
        return {}


def _full(article_id: str, 도배: int = 3_000_000, **extra) -> dict:
    """전체 리모델링 사례 — 도배·바닥·욕실이 있고 공종 6개."""
    return {"article_id": article_id, "region": "서울", "size_pyeong": 30, "total_cost": 40_000_000,
            "cost_도배": 도배, "cost_바닥": 4_000_000, "cost_욕실": 5_000_000, "cost_가구": 8_000_000,
            "cost_전기": 3_000_000, "cost_목공": 4_000_000, **extra}


def _partial(article_id: str, 도배: int = 2_000_000, **extra) -> dict:
    """부분 시공 사례 — 욕실과 바닥이 없다."""
    return {"article_id": article_id, "region": "서울", "size_pyeong": 30, "total_cost": 9_000_000,
            "cost_도배": 도배, "cost_전기": 1_000_000, **extra}


def _input(시공범위: str, 공종: list[str]) -> dict:
    return {
        "공종": 공종, "시공범위": 시공범위, "공간유형": "아파트", "평수": 30, "방개수": 3, "지역": "서울",
        "건물연식": "10~20년", "자재등급": "중급", "철거여부": "없음", "층수": 1, "엘리베이터": "있음",
        "트럭접근": "가능", "거주중공사": "공실", "공사시기": "미정",
    }


async def _generate(cases: list[dict], 시공범위: str, 공종: list[str]) -> tuple[dict, _Repo]:
    repo = _Repo(cases)
    engine = EstimateEngine(case_repository=repo, embedder=_Embedder(), reranker=None)
    return await engine.generate(_input(시공범위, 공종)), repo


def test_전체_리모델링_사례는_도배_바닥_욕실이_있고_공종이_6개_이상이다():
    assert EstimateEngine._is_partial_case(_full("1")) is False
    assert EstimateEngine._is_partial_case(_partial("2")) is True


def test_욕실_타일_설비는_공종_하나로_센다():
    # 도배·바닥에 욕실·타일·설비 — 키는 5개지만 공종은 3개라 부분 시공 사례다
    case = {"cost_도배": 1, "cost_바닥": 1, "cost_욕실": 1, "cost_타일": 1, "cost_설비": 1}
    assert EstimateEngine._is_partial_case(case) is True
    # 타일만 있어도 욕실 공종으로 본다
    case = {"cost_도배": 1, "cost_바닥": 1, "cost_타일": 1, "cost_가구": 1, "cost_전기": 1, "cost_목공": 1}
    assert EstimateEngine._is_partial_case(case) is False


def test_공종이_적어도_도배_바닥_욕실_중_하나가_빠지면_부분_시공_사례다():
    case = {"cost_도배": 1, "cost_욕실": 1, "cost_가구": 1, "cost_전기": 1, "cost_목공": 1, "cost_창호": 1, "cost_철거": 1}
    assert EstimateEngine._is_partial_case(case) is True  # 바닥이 없다


def _mixed(n_partial: int) -> list[dict]:
    """전체 리모델링 사례 3건과 부분 시공 사례 n건을 번갈아 섞은 후보."""
    partials = [_partial(f"p{i}") for i in range(n_partial)]
    fulls = [_full(f"f{i}") for i in range(3)]
    return [c for pair in zip(fulls, partials) for c in pair] + partials[3:]


def test_요청의_공종_구성이_전체_리모델링이면_부분_시공_요청으로_보지_않는다():
    assert EstimateEngine._is_partial_request(["도배", "가구"]) is True
    assert EstimateEngine._is_partial_request(["가구", "도배", "욕실", "전기/조명", "창호"]) is True  # 바닥이 없다
    # 시공범위를 "부분"으로 보내도 도배·바닥·욕실에 공종 6개면 전체 리모델링과 같은 구성이다
    assert EstimateEngine._is_partial_request(["가구", "도배", "마루", "욕실", "전기/조명", "창호"]) is False


async def test_부분_시공_요청은_부분_시공_사례만_참고한다():
    out, repo = await _generate(_mixed(PARTIAL_SCOPE_MIN_CASES), "부분", ["도배"])
    assert out["reference_case_ids"] == sorted(f"p{i}" for i in range(PARTIAL_SCOPE_MIN_CASES))
    assert out["공종별_단가_범위"]["도배"]["중간"] == 2_000_000  # 전체 리모델링 사례의 3,000,000이 섞이지 않는다
    assert repo.limits == [PARTIAL_SCOPE_POOL]  # 부분 시공 사례를 고르려고 후보를 넉넉히 가져온다


async def test_부분_시공_사례가_충분하지_않으면_전체_사례로_돌아간다():
    # 3~4건으로 좁히면 한 건에 크게 흔들리고 범위도 넓어진다
    cases = _mixed(PARTIAL_SCOPE_MIN_CASES - 1)
    out, repo = await _generate(cases, "부분", ["도배"])
    assert len(out["reference_case_ids"]) == len(cases)
    # 부분 시공 사례로 Stage 1~3을 돈 뒤, 기본 후보 풀로 다시 찾는다
    assert repo.limits == [PARTIAL_SCOPE_POOL] * 3 + [40]


async def test_부분_시공_사례로_좁히면_금액을_못_내는_공종이_생길_때는_좁히지_않는다():
    # 부분 시공 사례에는 가구 금액이 없다. 좁히면 가구가 빠져 총액이 그만큼 낮아진다
    cases = _mixed(PARTIAL_SCOPE_MIN_CASES)
    out, _ = await _generate(cases, "부분", ["도배", "가구"])
    assert len(out["reference_case_ids"]) == len(cases)
    assert "가구" in out["공종별_단가_범위"]


async def test_공종_구성이_전체_리모델링인_부분_요청은_사례를_가리지_않는다():
    out, repo = await _generate(_mixed(PARTIAL_SCOPE_MIN_CASES), "부분", ["가구", "도배", "마루", "욕실", "전기/조명", "창호"])
    assert repo.limits == [40]
    assert "f0" in out["reference_case_ids"]


async def test_전체_시공_요청은_사례를_가리지_않는다():
    out, repo = await _generate(_mixed(PARTIAL_SCOPE_MIN_CASES), "전체", ["도배", "가구"])
    assert repo.limits == [40]
    assert "f0" in out["reference_case_ids"]
