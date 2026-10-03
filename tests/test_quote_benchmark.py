"""
eval/quote_benchmark.py의 채점·요약·의뢰 제외 단위테스트 — DB와 모델 없이 도는 부분만.

채점 계산이 틀리면 엔진을 고칠 때마다 보는 수치가 전부 틀리므로 규칙을 테스트로 고정해 둔다. 금액은 지어낸 값이다.
"""

import statistics

import pytest

from eval.quote_benchmark import (
    LeaveOutCaseRepository,
    bootstrap_ci,
    print_report,
    score_range,
    score_record,
    summarize,
    summarize_scores,
)


def _record(**overrides) -> dict:
    record = {
        "id": "gt-test",
        "input": {"시공범위": "부분"},
        "flags": [],
        "truth": {"공종별": {"도배": 2_000_000, "욕실": 5_000_000}, "비교_총액": 8_000_000, "비교_직접비": 7_000_000},
    }
    record.update(overrides)
    return record


def _output(lo: int, mid: int, hi: int, **공종별) -> dict:
    return {"총_견적_범위": {"최소": lo, "중간": mid, "최대": hi}, "공종별_단가_범위": 공종별, "참고_사례_수": 12}


def test_score_range_오차율은_중간값_기준이고_폭은_중간값_대비다():
    s = score_range({"최소": 8_000_000, "중간": 10_000_000, "최대": 13_000_000}, 8_000_000)
    assert s["오차율"] == pytest.approx(0.25)
    assert s["적중"] is True  # 경계값도 적중
    assert s["폭"] == pytest.approx(0.5)


def test_score_range_정답이_범위_밖이면_벗어남():
    s = score_range({"최소": 8_000_000, "중간": 10_000_000, "최대": 13_000_000}, 14_000_000)
    assert s["적중"] is False
    assert s["오차율"] < 0


def test_score_range_범위가_없으면_None():
    assert score_range(None, 1_000_000) is None
    assert score_range({"최소": 1, "중간": 2, "최대": 3}, 0) is None


def test_score_record_총액은_두_기준으로_채점한다():
    row = score_record(_record(), _output(6_500_000, 7_500_000, 7_800_000))
    assert row["총액"]["적중"] is False  # 간접비 포함 8,000,000은 범위 밖
    assert row["직접비"]["적중"] is True  # 직접비 7,000,000은 범위 안


def test_score_record_엔진이_못_낸_공종은_None으로_남는다():
    output = _output(7_000_000, 8_000_000, 9_000_000, 도배={"최소": 1_500_000, "중간": 1_800_000, "최대": 2_200_000})
    row = score_record(_record(), output)
    assert row["공종별"]["도배"]["적중"] is True
    assert row["공종별"]["욕실"] is None


def test_score_record_엔진_오류는_실패로_남고_정답의_공종을_기록한다():
    row = score_record(_record(), {"error": "유사 사례를 찾을 수 없습니다."})
    assert row["실패"] == "유사 사례를 찾을 수 없습니다."
    assert row["공종"] == ["도배", "욕실"]
    assert "총액" not in row


def test_summarize_scores_미산출은_건수만_세고_지표에서_뺀다():
    scores = [
        {"오차율": 0.10, "적중": True, "폭": 0.4},
        {"오차율": -0.30, "적중": False, "폭": 0.6},
        {"오차율": -0.20, "적중": True, "폭": 0.5},
        None,
    ]
    s = summarize_scores(scores)
    assert (s["건수"], s["미산출"], s["실패"]) == (3, 1, 0)
    assert s["절대오차율_중앙값"] == pytest.approx(0.20)
    assert (s["과대"], s["과소"]) == (1, 2)
    assert s["적중률"] == pytest.approx(2 / 3)
    assert s["폭_중앙값"] == pytest.approx(0.5)


def test_summarize_scores_실패는_적중률에서_벗어남으로_센다():
    scores = [{"오차율": 0.10, "적중": True, "폭": 0.4}, {"오차율": -0.05, "적중": True, "폭": 0.4}]
    s = summarize_scores(scores, failed=2)
    assert s["적중률"] == pytest.approx(0.5)  # 4건 중 2건 — 실패를 빼면 100%로 보인다
    assert s["절대오차율_중앙값"] == pytest.approx(0.075)  # 오차율은 견적이 나온 건으로만
    assert (s["건수"], s["실패"]) == (2, 2)


def test_summarize_scores_채점할_것이_없으면_건수만():
    assert summarize_scores([None]) == {"건수": 0, "미산출": 1, "실패": 0}


def test_bootstrap_ci_같은_시드면_같은_구간이고_값_범위_안이다():
    values = [0.05, 0.10, 0.20, 0.35, 0.50]
    a = bootstrap_ci(values, statistics.median)
    assert a == bootstrap_ci(values, statistics.median)
    assert min(values) <= a[0] <= a[1] <= max(values)
    assert bootstrap_ci([0.1], statistics.median) is None


def test_summarize_시공범위와_플래그로_나누고_실패는_적중률_분모에_넣는다():
    rows = [
        score_record(_record(id="a"), _output(7_000_000, 8_000_000, 9_000_000)),
        score_record(_record(id="b", input={"시공범위": "전체"}, flags=["창호_도어만"]),
                     _output(9_000_000, 10_000_000, 11_000_000)),
        score_record(_record(id="c"), {"error": "없음"}),
    ]
    s = summarize(rows)
    assert s["실패"] == ["c"]
    assert (s["총액"]["전체"]["건수"], s["총액"]["전체"]["실패"]) == (2, 1)
    assert s["총액"]["전체"]["적중률"] == pytest.approx(1 / 3)  # a만 적중, b는 벗어남, c는 실패
    assert s["총액"]["전체 시공"]["건수"] == 1
    assert (s["총액"]["부분 시공"]["건수"], s["총액"]["부분 시공"]["실패"]) == (1, 1)
    assert s["총액"]["부분 시공"]["적중률"] == pytest.approx(0.5)
    assert (s["총액"]["플래그 없는 건"]["건수"], s["총액"]["플래그 없는 건"]["실패"]) == (1, 1)
    # 엔진이 공종 금액을 하나도 안 냈으므로 두 공종 모두 미산출 2건, 실패한 c도 공종별로 센다
    assert s["공종별"]["도배"] == {"건수": 0, "미산출": 2, "실패": 1}


def test_print_report_채점_불가와_실패가_섞여도_출력된다(capsys):
    truth = {"공종별": {"도배": 2_000_000}, "비교_총액": 8_000_000, "비교_직접비": 0}  # 직접비 기준은 채점 불가
    rows = [
        score_record(_record(id="a", truth=truth), _output(7_000_000, 8_000_000, 9_000_000)),
        score_record(_record(id="b"), {"error": "없음"}),
    ]
    print_report(rows, summarize(rows))
    out = capsys.readouterr().out
    assert "채점 불가" in out
    assert "실패: 없음" in out


# ── 의뢰 제외 ─────────────────────────────────────────────────────────────


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for doc in self._docs:
            yield doc


class _Collection:
    """벡터 순위대로 정렬된 코퍼스를 흉내 낸다. $vectorSearch의 limit만큼 앞에서 잘라 돌려준다."""

    def __init__(self, corpus):
        self.corpus = corpus
        self.find_queries = []
        self.pipelines = []

    def find(self, query, projection):
        self.find_queries.append(query)
        wanted = [cond for cond in query["$or"]]
        return _Cursor([d for d in self.corpus if any(all(d.get(k) == v for k, v in c.items()) for c in wanted)])

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        return _Cursor(self.corpus[:pipeline[0]["$vectorSearch"]["limit"]])


class _Settings:
    vector_index_name = "idx"


_CORPUS = [
    {"article_id": "1", "request_url": "u/1"},
    {"article_id": "2", "request_url": "u/9"},  # 정답 견적
    {"article_id": "3", "request_url": "u/9"},  # 같은 의뢰의 다른 업체 견적
    {"article_id": "4", "request_url": "u/4", "is_non_residential": True},
    {"article_id": "5", "request_url": "u/5"},
    {"article_id": "6", "request_url": "u/6"},
]


async def test_leave_out_같은_의뢰의_사례를_빼고_뺀_만큼_더_가져온다():
    collection = _Collection(_CORPUS)
    repo = LeaveOutCaseRepository(collection, _Settings())

    assert await repo.leave_out("u/9", "2") == 2
    assert collection.find_queries == [{"$or": [{"request_url": "u/9"}, {"article_id": "2"}]}]
    assert repo.left_out_ids == {"2", "3"}

    cases = await repo.vector_search([0.0], {"region": {"$eq": "서울"}}, 3)
    stage = collection.pipelines[0][0]["$vectorSearch"]
    assert stage["limit"] == 5  # 3건 요청 + 빠질 2건
    assert stage["filter"] == {"region": {"$eq": "서울"}}
    # 2·3을 뺀 순위는 1, 4, 5, 6. 서비스는 앞 3건(1, 4, 5)에서 비주거(4)를 걸러 2건을 돌려준다.
    # 비주거를 먼저 걸렀다면 6이 빈자리를 채워 서비스에 없는 사례가 들어온다
    assert [c["article_id"] for c in cases] == ["1", "5"]


async def test_leave_out_출처가_없으면_아무것도_빼지_않는다():
    collection = _Collection(_CORPUS)
    repo = LeaveOutCaseRepository(collection, _Settings())

    assert await repo.leave_out(None, None) == 0
    assert collection.find_queries == []  # 조건 없이 조회하면 코퍼스 전체가 제외 대상이 된다
    cases = await repo.vector_search([0.0], None, 3)
    assert collection.pipelines[0][0]["$vectorSearch"]["limit"] == 3
    assert "filter" not in collection.pipelines[0][0]["$vectorSearch"]
    assert [c["article_id"] for c in cases] == ["1", "2", "3"]


async def test_leave_out_다음_레코드로_넘어가면_이전_제외는_풀린다():
    repo = LeaveOutCaseRepository(_Collection(_CORPUS), _Settings())
    await repo.leave_out("u/9", "2")
    await repo.leave_out("u/5", "5")
    assert repo.left_out_ids == {"5"}
