"""
참고 사례가 적게 모이던 원인들의 단위테스트 — 자재등급으로 거르지 않는다, 사례 총금액에 철거비를 또 더하지 않는다,
품목 명세는 정규화된 공종 이름으로 찾는다.

실제 사례: material_grade가 있는 사례는 코퍼스의 9%뿐인데(700건 중 65건) 첫 단계가 등급으로 걸러서, "서울 30평
전체 시공"이 사례 4건·범위 폭 79%로 나왔다. 고급 요청은 고급 사례만 고른 뒤 고급 계수(×1.25)를 또 곱해
서울 30평이 7,781만 원(등급을 한 번만 반영하면 5,638만 원)으로 나왔다. 금액은 지어낸 값이다.
"""

import json

from app.domain.estimate_engine import EstimateEngine


class _Vector:
    def tolist(self):
        return [0.0]


class _Embedder:
    def encode(self, query):
        return _Vector()


class _Repo:
    def __init__(self, cases, docs=None):
        self._cases = cases
        self._docs = docs or {}
        self.filters = []

    async def count(self):
        return 700

    async def vector_search(self, query_embedding, mongo_filter, limit, num_candidates=150):
        self.filters.append(mongo_filter)
        return self._cases[:limit]

    async def find_by_article_ids(self, article_ids):
        return self._docs


def _case(i: int, total: int = 40_000_000) -> dict:
    return {"article_id": str(i), "region": "서울", "size_pyeong": 30, "total_cost": total,
            "cost_도배": total // 4, "cost_바닥": total // 8, "cost_욕실": total // 8, "cost_가구": total // 8,
            "cost_전기": total // 16, "cost_목공": total // 16, "cost_철거": total // 16}


def _input(**overrides) -> dict:
    inp = {
        "공종": ["도배", "장판", "욕실", "가구", "전기/조명", "목공"], "시공범위": "전체", "공간유형": "아파트",
        "평수": 30, "방개수": 3, "지역": "서울", "건물연식": "10~20년", "자재등급": "중급", "철거여부": "없음",
        "층수": 1, "엘리베이터": "있음", "트럭접근": "가능", "거주중공사": "공실", "공사시기": "미정",
    }
    inp.update(overrides)
    return inp


def _engine(repo: _Repo) -> EstimateEngine:
    return EstimateEngine(case_repository=repo, embedder=_Embedder(), reranker=None)


# ── 자재등급 ──────────────────────────────────────────────────────────────


async def test_자재등급으로는_사례를_거르지_않는다():
    repo = _Repo([_case(i) for i in range(15)])
    await _engine(repo).generate(_input(자재등급="고급"))

    assert repo.filters, "검색이 한 번은 일어나야 한다"
    assert all("material_grade" not in json.dumps(f, ensure_ascii=False) for f in repo.filters)


async def test_자재등급은_계수로_한_번만_반영한다():
    repo = _Repo([_case(i) for i in range(15)])
    중급 = await _engine(repo).generate(_input(자재등급="중급"))
    고급 = await _engine(repo).generate(_input(자재등급="고급"))

    assert 중급["총_견적_범위"]["중간"] == 40_000_000
    assert 고급["총_견적_범위"]["중간"] == 50_000_000  # 같은 사례에 ×1.25


async def test_첫_단계는_평수_지역_공종_조건이다():
    repo = _Repo([_case(i) for i in range(15)])
    await _engine(repo).generate(_input())

    first = json.dumps(repo.filters[0], ensure_ascii=False)
    assert "size_pyeong" in first and "region" in first and "has_도배" in first


# ── 철거비 보정 ────────────────────────────────────────────────────────────


async def test_전체_시공은_사례_총금액에_철거비를_또_더하지_않는다():
    # 공종에 철거가 없고 철거여부가 "있음"이면 평당 25,000원을 더해 왔다. 사례 총금액에는 철거공사가 이미 들어 있다
    repo = _Repo([_case(i) for i in range(15)])
    out = await _engine(repo).generate(_input(철거여부="있음"))

    assert out["총_견적_범위"]["중간"] == 40_000_000
    assert not any("철거비 보정" in note for note in out["보정_적용"])


async def test_부분_시공은_공종_합에_철거비를_더한다():
    repo = _Repo([_case(i) for i in range(15)])
    out = await _engine(repo).generate(_input(시공범위="부분", 공종=["도배"], 철거여부="있음"))

    assert out["총_견적_범위"]["중간"] == 10_000_000 + 25_000 * 30
    assert "철거비 보정 +750,000원" in out["보정_적용"]


async def test_공종에_철거가_있으면_철거비를_더하지_않는다():
    repo = _Repo([_case(i) for i in range(15)])
    out = await _engine(repo).generate(_input(시공범위="부분", 공종=["도배", "철거"], 철거여부="있음"))

    assert out["총_견적_범위"]["중간"] == 10_000_000 + 2_500_000
    assert not any("철거비 보정" in note for note in out["보정_적용"])


# ── 품목 명세 ──────────────────────────────────────────────────────────────


def _docs(category: str) -> dict:
    return {str(i): {"parsed_estimate": {"line_items": [
        {"category": category, "description": desc, "amount": amount}]}}
        # 표기는 다르지만 둘 다 "샷시/새시"로 정리되는 품명이다
        for i, (desc, amount) in enumerate([("KCC 샷시 22mm", 6_000_000), ("영림샷시 (115mm)", 5_000_000)])}


async def test_품목의_공종이_정규화된_이름이어도_명세에_나온다():
    # 코퍼스 품목의 공종은 대부분 "창호공사"가 아니라 "창호"다. 견적서 표기로만 찾으면 명세가 비어서 나온다
    cases = [{"article_id": "0"}, {"article_id": "1"}]
    for category in ("창호", "창호공사"):
        engine = _engine(_Repo(cases, _docs(category)))
        spec = await engine.collect_line_items(cases, ["창호"])
        assert len(spec["창호"]) == 1, category


def test_품명_정리_규칙을_정규화된_공종_이름으로도_찾는다():
    # 규칙의 키는 "조명공사"지만 품목의 공종은 "전기"로 저장돼 있다
    assert EstimateEngine._normalize_desc("전기", "거실 다운라이트 3인치") == ("다운라이트", True)
    assert EstimateEngine._normalize_desc("조명공사", "거실 다운라이트 3인치") == ("다운라이트", True)
    assert EstimateEngine._normalize_desc("창호", "현관중문 3연동") == ("중문", True)


def test_견적서_표기가_남은_품목은_그_공종의_규칙만_쓴다():
    # 수전공사·도기공사·설비공사는 모두 "설비"로 정규화된다. 규칙을 섞으면 수전공사의 "세면기 수전"이
    # 도기공사의 "세면기" 규칙에 걸려 세면기 금액과 한 줄로 묶인다
    assert EstimateEngine._normalize_desc("수전공사", "세면기 수전")[0] != "세면기"
    assert EstimateEngine._normalize_desc("조명공사", "다운라이트 설치 인건비") == ("다운라이트", True)


def test_정규화된_공종의_품목은_구체적인_규칙이_인건비_부자재보다_먼저다():
    # "전기"에는 전기공사와 조명공사의 규칙이 이어진다. 앞 목록의 "인건비"가 뒤 목록의 "다운라이트"를 가로채면 안 된다
    assert EstimateEngine._normalize_desc("전기", "다운라이트 설치 인건비") == ("다운라이트", True)
    assert EstimateEngine._normalize_desc("전기", "인건비") == ("인건비", True)


async def test_도어공사에_적힌_가구_문짝은_창호_명세에_넣지_않는다():
    cases = [{"article_id": "0"}, {"article_id": "1"}]
    docs = {aid: {"parsed_estimate": {"line_items": [
        {"category": "도어공사", "description": "붙박이장 문짝 교체", "amount": 900_000},
        {"category": "창호", "description": "KCC 샷시 22mm", "amount": 6_000_000}]}} for aid in ("0", "1")}
    spec = await _engine(_Repo(cases, docs)).collect_line_items(cases, ["창호"])

    assert [row["description"] for row in spec["창호"]] == ["샷시/새시"]


async def test_공종_없이_부르는_리스크_진단의_검색도_자재등급으로_거르지_않는다():
    # 가격 비교는 자재등급 "중급" 고정으로 사례 검색을 부른다. 등급이 있는 사례 3~5건이 아니라 같은 지역·평수의 사례와 비교한다
    repo = _Repo([_case(i) for i in range(15)])
    cases = await _engine(repo).retrieve_cases("30평 서울 리모델링", {"평수": 30, "지역": "서울", "자재등급": "중급", "시공범위": "부분", "공종": []})

    assert len(cases) == 15
    assert all("material_grade" not in json.dumps(f, ensure_ascii=False) for f in repo.filters)


async def test_조건을_다_풀어도_모자라면_조건_없이_한_번만_찾는다():
    repo = _Repo([_case(0), _case(1)])  # 어느 단계에서도 3건이 안 된다
    cases = await _engine(repo).retrieve_cases("30평 서울 리모델링", _input())

    assert len(cases) == 2
    assert repo.filters[-1] is None and repo.filters.count(None) == 1
    assert len(repo.filters) == 4  # 평수+지역+공종 → 지역 완화 → 평수 완화 → 조건 없음
