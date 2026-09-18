"""Stage 필터가 이미 후보를 좁혀놓은 상태에서, 하이브리드(BM25+RRF)+크로스인코더
리랭킹이 여전히 순효과가 있는지 분리해서 잰다.

retrieve_cases()의 _hybrid_rerank()는 새 후보를 더 가져오는 게 아니라, 이미
Stage 필터를 통과해 vector_search()로 가져온 같은 후보 집합(최대 vector_candidate_pool
개)을 재정렬만 한다 — 그래서 "필터+벡터 단독"과 "필터+하이브리드+리랭킹"은 정확히
같은 후보 풀에서 순서만 다르게 top-15를 고르는 것과 같다. 후보가 이미 15개 이하인
쿼리는 재정렬 자체가 무의미(둘 다 동일 결과)하다는 점도 같이 확인한다.

실행: python -m eval.production_rerank_ablation_eval
사전 준비: eval.production_retrieval_eval과 동일 — labels.csv가 대상 후보를 전량
          커버하고 있어야 한다(커버 안 되면 eval.label_production_candidates로 보강).
"""

import asyncio
import csv
import json
import pathlib

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import REGION_MAP, 자재등급_TO_GRADE, EstimateEngine
from app.repositories.case_repository import CaseRepository

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"

STAGE_CONFIGS = [
    (True, True, True, True),
    (True, True, True, False),
    (True, False, True, False),
    (False, False, True, False),
    (False, False, False, False),
]


async def resolve_filtered_candidates(engine: EstimateEngine, repo: CaseRepository, inp: dict, pool_n: int):
    """retrieve_cases()와 동일한 Stage 폴백으로 필터링된 후보(재정렬 전, 벡터 유사도
    내림차순 그대로)를 반환한다."""
    query_text = engine.build_query(inp)
    query_embedding = (await asyncio.get_event_loop().run_in_executor(None, engine._embedder.encode, query_text)).tolist()

    평수 = int(inp.get("평수") or 0)
    지역들 = REGION_MAP.get(inp.get("지역", "서울"), ["서울"])
    공종들 = inp.get("공종", [])
    grade = 자재등급_TO_GRADE.get(inp.get("자재등급", "중급"), "중급")

    for use_size, use_region, use_has, use_grade in STAGE_CONFIGS:
        mongo_filter = engine._build_filter(
            평수, 지역들, 공종들,
            use_size=use_size, use_region=use_region, use_has=use_has,
            use_grade=use_grade, grade=grade,
        )
        cases = await repo.vector_search(query_embedding, mongo_filter, pool_n)
        if len(cases) >= 3:
            return query_text, cases

    cases = await repo.vector_search(query_embedding, None, pool_n)
    return query_text, cases


def precision(cases: list[dict], labels: dict, qid: str) -> tuple[int, int, int]:
    labeled = relevant = unlabeled = 0
    for c in cases[:15]:
        key = (qid, str(c.get("article_id")))
        if key in labels:
            labeled += 1
            relevant += labels[key]
        else:
            unlabeled += 1
    return labeled, relevant, unlabeled


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]
    repo = CaseRepository(col, settings)

    embedder = SentenceTransformer(settings.embed_model)
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    engine = EstimateEngine(case_repository=repo, embedder=embedder, reranker=reranker)

    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    labels: dict[tuple, int] = {}
    with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            labels[(row["query_id"], row["article_id"])] = int(row["label"])

    total = await repo.count()
    pool_n = min(engine._vector_candidate_pool, total) if total else engine._vector_candidate_pool

    vec_labeled = vec_relevant = vec_unlabeled = 0
    hyb_labeled = hyb_relevant = hyb_unlabeled = 0
    identical_count = 0

    print(f"{'query':<6}{'후보풀':>8}{'vec@15':>10}{'hyb@15':>10}{'동일여부':>8}")
    for q in queries:
        qid = q["query_id"]
        inp = q["input"]
        query_text, cases = await resolve_filtered_candidates(engine, repo, inp, pool_n)

        vector_top15 = cases[:15]
        hybrid_top15 = (await engine._hybrid_rerank(query_text, cases))[:15]

        vl, vr, vu = precision(vector_top15, labels, qid)
        hl, hr, hu = precision(hybrid_top15, labels, qid)
        vec_labeled += vl
        vec_relevant += vr
        vec_unlabeled += vu
        hyb_labeled += hl
        hyb_relevant += hr
        hyb_unlabeled += hu

        same_ids = [str(c.get("article_id")) for c in vector_top15] == [str(c.get("article_id")) for c in hybrid_top15]
        if same_ids:
            identical_count += 1

        v_str = f"{vr}/{vl}" if vl else "N/A"
        h_str = f"{hr}/{hl}" if hl else "N/A"
        print(f"{qid:<6}{len(cases):>8}{v_str:>10}{h_str:>10}{'동일' if same_ids else '다름':>8}")

    client.close()

    print()
    print(f"동일한 top-15를 반환한 쿼리: {identical_count}/{len(queries)}개 (후보가 15개 이하라 재정렬이 무의미했던 경우 포함)")
    print(f"라벨 커버리지 — 필터+벡터단독: {vec_labeled}/{vec_labeled+vec_unlabeled}, 필터+하이브리드: {hyb_labeled}/{hyb_labeled+hyb_unlabeled}")
    if vec_labeled:
        print(f"필터 + 벡터 단독      precision@15: {vec_relevant/vec_labeled:.1%}")
    if hyb_labeled:
        print(f"필터 + 하이브리드+리랭킹 precision@15: {hyb_relevant/hyb_labeled:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
