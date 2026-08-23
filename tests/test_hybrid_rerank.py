"""
RRF(Reciprocal Rank Fusion) 순수 로직 + 리랭커 OFF 경로 단위테스트.

cross-encoder를 실제로 로드하지 않고도 검증 가능한 부분만 테스트한다. 리랭커를 켠
_hybrid_rerank()의 cross-encoder 분기는 여기서 다루지 않는다 — CI에서 약 2.2GB짜리
reranker를 매번 내려받게 하고 싶지 않기 때문. 실동작 검증은 로컬에서 실제 Mongo/모델로
수행한다. (reranker=None 분기는 모델이 필요 없으므로 아래에서 검증한다.)
"""

import pytest

from app.domain.estimate_engine import TOP_K, EstimateEngine


def test_rrf_favors_items_ranked_high_in_both_lists():
    vector_rank = ["a", "b", "c", "d"]
    bm25_rank = ["b", "a", "d", "c"]

    scores = EstimateEngine._reciprocal_rank_fusion([vector_rank, bm25_rank])

    # a, b는 두 목록 모두에서 상위권 → c, d보다 RRF 점수가 높아야 함
    assert scores["a"] > scores["c"]
    assert scores["b"] > scores["d"]


def test_rrf_single_list_preserves_order():
    ranking = ["x", "y", "z"]
    scores = EstimateEngine._reciprocal_rank_fusion([ranking])

    ordered = sorted(scores, key=scores.get, reverse=True)
    assert ordered == ranking


def test_rrf_item_missing_from_one_list_still_scored():
    vector_rank = ["a", "b"]
    bm25_rank = ["b", "c"]  # a는 BM25 목록에 없음

    scores = EstimateEngine._reciprocal_rank_fusion([vector_rank, bm25_rank])

    assert set(scores.keys()) == {"a", "b", "c"}
    # b는 두 목록 모두에 있어 a, c보다 점수가 높아야 함
    assert scores["b"] > scores["a"]
    assert scores["b"] > scores["c"]


# ── 리랭커 OFF(settings.use_reranker=False) 경로 ──────────────────────────


def _engine_without_reranker() -> EstimateEngine:
    """_hybrid_rerank()는 case_repository/embedder를 쓰지 않으므로 None으로 둔다."""
    return EstimateEngine(case_repository=None, embedder=None, reranker=None)


def _fake_cases(n: int) -> list[dict]:
    return [
        {
            "article_id": f"case{i}",
            "size_pyeong": 30,
            "region": "서울",
            "has_도배": "true",
            "request_body_text": f"30평 아파트 도배 시공 사례 {i}",
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_hybrid_rerank_without_reranker_returns_top_k():
    """리랭커가 없어도 하이브리드(벡터+BM25/RRF)까지는 동작하고 TOP_K를 반환해야 한다."""
    engine = _engine_without_reranker()
    cases = _fake_cases(TOP_K + 10)

    result = await engine._hybrid_rerank("30평 서울 도배 리모델링", cases)

    assert len(result) == TOP_K
    # 원본 후보에서만 골라야 하고, 중복이 없어야 한다
    ids = [c["article_id"] for c in result]
    assert len(set(ids)) == len(ids)
    assert set(ids) <= {c["article_id"] for c in cases}


@pytest.mark.asyncio
async def test_hybrid_rerank_without_reranker_skips_when_pool_small():
    """후보가 TOP_K 이하면 리랭킹 의미가 없어 그대로 반환한다 (리랭커 유무와 무관)."""
    engine = _engine_without_reranker()
    cases = _fake_cases(TOP_K - 1)

    result = await engine._hybrid_rerank("30평 서울 도배 리모델링", cases)

    assert result == cases
