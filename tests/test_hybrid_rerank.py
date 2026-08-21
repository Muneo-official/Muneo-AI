"""
RRF(Reciprocal Rank Fusion) 순수 로직 단위테스트.

cross-encoder/BM25 모델을 실제로 로드하지 않고도 검증 가능한 부분만 테스트한다.
모델이 필요한 _hybrid_rerank() 자체는 여기서 다루지 않는다 — CI에서 수백 MB짜리
reranker를 매번 내려받게 하고 싶지 않기 때문. 실동작 검증은 로컬에서 실제 Mongo/모델로
수행한다.
"""

from app.domain.estimate_engine import EstimateEngine


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
