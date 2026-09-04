"""cross-encoder(Dongjin-kr/ko-reranker)가 텍스트 길이 자체에 편향되는지 통제 실험.

배경: eval/results/reranker_hybrid_eval.md에서 BM25는 최종 순위에 영향이 없었고(RERANK_POOL=20
구조상), 실제로 순위를 흔든 건 cross-encoder라는 게 확인됐다. cross-encoder가 상세히 쓴 요청글을
더 관련 있다고 채점하는 경향(길이 편향)이 있는지, 아니면 단순히 더 긴 글에 실제로 더 많은 관련
정보가 들어있어서 정당하게 높은 점수를 받는지는 구분이 안 된 상태였다.

방법: 실제로 "밀려난" 짧은 사례 5건의 원문에 **의미 없는 필러 문장**(견적과 무관한 인사말)을
덧붙여 인위적으로 길이만 늘린 버전을 만든다. 진짜 정보량은 그대로인데 길이만 늘었을 때 CE 점수가
오르면, 이건 "정보가 많아서"가 아니라 "글자 수 자체"에 대한 편향이라는 뜻이다.
"""

import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine

load_dotenv()

# eval/results/reranker_hybrid_eval.md에서 반복적으로 top-5 밖으로 밀려난 것으로 확인된 사례들과,
# 그 사례가 실제로 후보로 등장했던 쿼리(eval/test_inputs/pool.json 기준) 중 하나.
DEMOTED_CASES = [
    ("879345", "q01"),
    ("882159", "q04"),
    ("882169", "q04"),
    ("883238", "q03"),
    ("884553", "q07"),
]

# 견적/공사와 무관한 필러 문장 — 실제 견적 의뢰 게시글에서도 흔히 붙는 상투적 끝인사라
# 부자연스럽지 않으면서, 평수·지역·공종 같은 매칭에 쓰일 정보는 전혀 담겨 있지 않다.
FILLER = (
    "빠른 답변 부탁드립니다. 성실하게 시공해주실 업체 찾고 있습니다. "
    "가격도 가격이지만 꼼꼼하게 작업해주시는 게 제일 중요하다고 생각해요. "
    "여러 업체 비교해보고 결정하려고 하니 참고 부탁드립니다. "
    "연락 기다리겠습니다. 감사합니다. "
)


def _pad_to_length(text: str, target_len: int) -> str:
    padded = text
    while len(padded) < target_len:
        padded += " " + FILLER
    return padded[:target_len] if len(padded) > target_len else padded


async def main() -> None:
    import json
    import pathlib

    base = pathlib.Path(__file__).parent / "test_inputs"
    queries = {q["query_id"]: q for q in json.loads((base / "queries.json").read_text(encoding="utf-8"))}

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    cases_col = client[settings.mongo_db_name]["estimate_cases"]
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    engine = EstimateEngine(case_repository=None, embedder=None, reranker=reranker)

    print(f"{'article_id':<12}{'query':<8}{'원본길이':>8}{'원본점수':>12}{'패딩길이':>8}{'패딩점수':>12}{'변화':>10}")
    print("-" * 70)

    for article_id, query_id in DEMOTED_CASES:
        case = await cases_col.find_one({"article_id": article_id}, {"embedding": 0})
        if case is None:
            print(f"{article_id}: 사례를 찾을 수 없음 (스킵)")
            continue

        query_text = engine.build_query(queries[query_id]["input"])
        original_text = EstimateEngine._case_text(case)
        padded_text = _pad_to_length(original_text, 560)  # 끌어올려진 사례들의 평균 길이(560자)로 맞춤

        scores = await run_in_threadpool(
            reranker.predict, [(query_text, original_text), (query_text, padded_text)]
        )
        orig_score, pad_score = float(scores[0]), float(scores[1])
        delta = pad_score - orig_score
        print(
            f"{article_id:<12}{query_id:<8}{len(original_text):>8}{orig_score:>12.4f}"
            f"{len(padded_text):>8}{pad_score:>12.4f}{delta:>+10.4f}"
        )

    client.close()


if __name__ == "__main__":
    asyncio.run(main())
