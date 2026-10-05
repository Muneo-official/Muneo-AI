"""
전체 시공 총액의 단위테스트 — 사례의 총금액이 아니라, 요청한 공종의 공종별 중간값을 더한 값으로 낸다.

실제 사례: 전체 시공 총액을 참고 사례의 총금액 중앙값으로 내서, 요청에 없는 공종이 총액에 그대로 들어갔다.
창호를 요청하지 않았는데 참고 사례 13건 중 10건이 창호(총금액의 21%)를 포함해 총액이 크게 높게 나온 건이 있었다.
확장공사와 이윤·보험료도 같은 길로 들어갔다. 금액은 지어낸 값이다.
"""

from app.domain.estimate_engine import EstimateEngine


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
        return 700

    async def vector_search(self, query_embedding, mongo_filter, limit, num_candidates=150):
        return self._cases[:limit]

    async def find_by_article_ids(self, article_ids):
        return {}


def _case(i: int, **overrides) -> dict:
    # 공종 금액의 합 4,400만 + 확장 300만 = 4,700만. 총금액은 이윤·보험료 300만을 더한 5,000만
    case = {
        "article_id": str(i), "region": "서울", "size_pyeong": 30, "total_cost": 50_000_000,
        "cost_도배": 4_000_000, "cost_바닥": 5_000_000, "cost_욕실": 4_000_000, "cost_타일": 2_000_000,
        "cost_설비": 1_000_000, "cost_가구": 8_000_000, "cost_전기": 3_000_000, "cost_목공": 4_000_000,
        "cost_철거": 3_000_000, "cost_창호": 8_000_000, "cost_공과잡비": 2_000_000, "cost_확장": 3_000_000,
    }
    case.update(overrides)
    return case


기본_공종 = ["도배", "장판", "욕실", "가구", "전기/조명", "목공", "철거"]
기본_합 = 4_000_000 + 5_000_000 + 7_000_000 + 8_000_000 + 3_000_000 + 4_000_000 + 3_000_000  # 3,400만
공과잡비 = 2_000_000


def _input(공종: list[str], **overrides) -> dict:
    inp = {
        "공종": 공종, "시공범위": "전체", "공간유형": "아파트", "평수": 30, "방개수": 3, "지역": "서울",
        "건물연식": "10~20년", "자재등급": "중급", "철거여부": "없음", "층수": 1, "엘리베이터": "있음",
        "트럭접근": "가능", "거주중공사": "공실", "공사시기": "미정",
    }
    inp.update(overrides)
    return inp


async def _generate(cases: list[dict], 공종: list[str], **overrides) -> dict:
    engine = EstimateEngine(case_repository=_Repo(cases), embedder=_Embedder(), reranker=None)
    return await engine.generate(_input(공종, **overrides))


async def test_요청하지_않은_공종은_총액에_들어가지_않는다():
    cases = [_case(i) for i in range(15)]
    창호_없이 = await _generate(cases, 기본_공종)
    창호_포함 = await _generate(cases, 기본_공종 + ["창호"])

    # 사례의 총금액(5,000만)이 아니라 요청한 공종의 금액 — 창호·확장·이윤이 빠진다
    assert 창호_없이["총_견적_범위"]["중간"] == 기본_합 + 공과잡비
    assert 창호_포함["총_견적_범위"]["중간"] == 기본_합 + 공과잡비 + 8_000_000


async def test_요청한_공종을_하지_않은_사례가_총액을_낮추지_않는다():
    # 참고 사례 15건 중 9건이 샷시를 하지 않았다. 사례마다 요청 공종의 금액을 더해 중앙값을 내면 샷시가 0원으로
    # 들어간 합이 중앙값이 된다. 총액은 공종별 중간값의 합이고, 샷시의 중간값은 샷시를 한 사례에서 구한다
    cases = [_case(i, cost_창호=0 if i < 9 else 8_000_000) for i in range(15)]
    out = await _generate(cases, 기본_공종 + ["창호"])

    assert out["총_견적_범위"]["중간"] == 기본_합 + 공과잡비 + 8_000_000


async def test_총액은_보여_주는_공종별_중간값의_합이다():
    cases = [_case(i, cost_도배=4_000_000 + i * 100_000, cost_가구=8_000_000 - i * 300_000) for i in range(15)]
    out = await _generate(cases, 기본_공종)

    assert out["총_견적_범위"]["중간"] == sum(r["중간"] for r in out["공종별_단가_범위"].values())
    assert "마감/공과잡비" in out["공종별_단가_범위"]


async def test_마감_공과잡비는_사례의_금액으로_총액에_들어가고_같은_금액으로_표시한다():
    out = await _generate([_case(i) for i in range(15)], 기본_공종)

    assert out["공종별_단가_범위"]["마감/공과잡비"] == {"최소": 공과잡비, "중간": 공과잡비, "최대": 공과잡비}
    assert "마감/공과잡비는 참고 사례의 금액으로 총액에 포함" in out["보정_적용"]
    assert "마감/공과잡비" in out["선택_공종"]


async def test_주방과_가구를_함께_골라도_가구_금액은_한_번만_센다():
    cases = [_case(i) for i in range(15)]
    기준 = (await _generate(cases, 기본_공종))["총_견적_범위"]["중간"]

    # 주방·가구는 사례의 가구 금액 하나를 60/40으로 나눠 보여 준다
    assert (await _generate(cases, 기본_공종 + ["주방"]))["총_견적_범위"]["중간"] == 기준


async def test_공종별_보정은_총액에도_반영한다():
    cases = [_case(i) for i in range(15)]
    out = await _generate(cases, 기본_공종, 도배={"범위": ["거실"], "도배지종류": "실크벽지"})

    assert out["총_견적_범위"]["중간"] == 기본_합 + 공과잡비 - int(4_000_000 * 0.65)  # 도배는 거실(35%)만


async def test_공과잡비_금액이_있는_사례가_적으면_마감은_비율로_낸다():
    # 15건 중 1건에만 공과잡비가 있고 그 금액이 크다. 한 건의 금액을 마감으로 쓰지 않는다
    cases = [_case(i, cost_공과잡비=15_000_000 if i == 0 else 0) for i in range(15)]
    out = await _generate(cases, 기본_공종)

    assert out["총_견적_범위"]["중간"] == int(기본_합 * 1.03)
    assert "마감/공과잡비 포함 (총 공사비의 3%)" in out["보정_적용"]


async def test_마감_비율이_0이면_사례의_공과잡비도_더하지_않는다():
    engine = EstimateEngine(case_repository=_Repo([_case(i) for i in range(15)]), embedder=_Embedder(),
                            reranker=None, coefficients={"finishing_ratio": 0})
    out = await engine.generate(_input(기본_공종))

    assert out["총_견적_범위"]["중간"] == 기본_합
    assert "마감/공과잡비" not in out["공종별_단가_범위"]


철거_뺀_공종 = [g for g in 기본_공종 if g != "철거"]


async def test_철거비_보정과_양중비는_공종별_금액의_합에_더해진다():
    out = await _generate([_case(i) for i in range(15)], 철거_뺀_공종, 철거여부="모름", 엘리베이터="없음", 층수=5)

    공종_합 = sum(r["중간"] for r in out["공종별_단가_범위"].values())
    assert out["총_견적_범위"]["중간"] == 공종_합 + 12_000 * 30 + 150_000 * 5


async def test_철거가_있다고_한_전체_시공은_공종에_철거가_없어도_사례의_철거_금액을_더한다():
    # 평당 보정액(30평 75만)이 아니라 참고 사례의 철거 금액(300만). 공종에 철거를 넣은 요청과 총액이 같다
    cases = [_case(i) for i in range(15)]
    out = await _generate(cases, 철거_뺀_공종, 철거여부="있음")

    assert out["총_견적_범위"]["중간"] == (await _generate(cases, 기본_공종, 철거여부="있음"))["총_견적_범위"]["중간"]
    assert "철거비 +3,000,000원 (참고 사례의 철거 금액)" in out["보정_적용"]
    assert "철거" not in out["공종별_단가_범위"]


async def test_철거_금액이_있는_사례가_적으면_평당_보정액을_쓴다():
    cases = [_case(i, cost_철거=3_000_000 if i < 2 else 0) for i in range(15)]
    out = await _generate(cases, 철거_뺀_공종, 철거여부="있음")

    assert "철거비 보정 +750,000원" in out["보정_적용"]


async def test_장판과_마루를_함께_골라도_바닥_금액은_총액에_한_번만_들어간다():
    cases = [_case(i) for i in range(15)]
    기준 = (await _generate(cases, 기본_공종))["총_견적_범위"]["중간"]
    out = await _generate(cases, 기본_공종 + ["마루"])

    assert out["총_견적_범위"]["중간"] == 기준
    assert "장판·마루는 같은 바닥 금액이라 총액에 한 번만 포함" in out["보정_적용"]
    assert (await _generate(cases, ["장판", "마루"], 시공범위="부분"))["총_견적_범위"]["중간"] == 5_000_000


async def test_금액이_없는_요청_공종은_총액에_빠졌다고_알린다():
    cases = [_case(i, cost_필름=0) for i in range(15)]
    out = await _generate(cases, 기본_공종 + ["필름"])

    assert out["데이터_부족_공종"] == ["필름"]
    assert "필름 금액은 참고 사례에 없어 총액에 포함되지 않음" in out["보정_적용"]


async def test_요청_공종의_금액을_가진_사례가_없으면_사례의_총금액으로_내고_범위는_전체_시공_마진을_쓴다():
    cases = [{"article_id": str(i), "region": "서울", "size_pyeong": 30, "total_cost": 30_000_000 + i * 3_000_000}
             for i in range(15)]
    총 = (await _generate(cases, 기본_공종))["총_견적_범위"]

    assert 총["중간"] == 51_000_000
    assert 총["최소"] == int(51_000_000 * 0.96)
    assert 총["최대"] == int(51_000_000 * 1.23)
