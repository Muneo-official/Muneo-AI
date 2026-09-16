"""EstimateEngine.retrieve_cases()(Stage 1~5 점진적 필터 + 하이브리드 리랭킹, 실제
production 경로 그대로)의 최종 top-15가 실제로 얼마나 정확한지 측정한다.

eval/retrieval_eval.py와는 다른 걸 잰다 — 그쪽은 "필터 없는 벡터 검색 top-20" 안에서
벡터 단독 vs 하이브리드+리랭킹을 비교하는 것이고(production의 _hybrid_rerank() 재료는
같지만 평수/지역/공종 필터가 빠져있음), 이건 Stage 필터까지 포함한 진짜
retrieve_cases() 결과 자체의 precision을 잰다.

실행: python -m eval.production_retrieval_eval
사전 준비: eval/label_production_candidates.py로 뽑은 신규 후보를 라벨링하고
          eval/apply_production_labels.py로 labels.csv에 반영해둘 것 — 안 그러면
          "라벨 없음"으로 커버리지가 낮게 나온다.
"""

import asyncio
import csv
import json
import pathlib

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from sentence_transformers import CrossEncoder, SentenceTransformer

from app.core.config import get_settings
from app.domain.estimate_engine import EstimateEngine
from app.repositories.case_repository import CaseRepository

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]
    repo = CaseRepository(col, settings)

    embedder = SentenceTransformer(settings.embed_model)
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    engine = EstimateEngine(case_repository=repo, embedder=embedder, reranker=reranker)

    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    labels: dict[tuple[str, str], int] = {}
    with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            labels[(row["query_id"], row["article_id"])] = int(row["label"])

    total_labeled = total_relevant = total_unlabeled = 0
    per_query = []

    for q in queries:
        qid = q["query_id"]
        inp = q["input"]
        query_text = engine.build_query(inp)
        cases = await engine.retrieve_cases(query_text, inp)
        top15 = cases[:15]

        labeled = relevant = 0
        unlabeled_ids = []
        for c in top15:
            aid = str(c.get("article_id"))
            key = (qid, aid)
            if key in labels:
                labeled += 1
                relevant += labels[key]
            else:
                unlabeled_ids.append(aid)

        total_labeled += labeled
        total_relevant += relevant
        total_unlabeled += len(unlabeled_ids)
        precision = relevant / labeled if labeled else None
        per_query.append((qid, len(top15), labeled, relevant, precision, unlabeled_ids))

    client.close()

    print(f"{'query':<6}{'후보수':>6}{'라벨있음':>8}{'relevant':>10}{'precision':>12}")
    for qid, n, labeled, relevant, precision, unlabeled_ids in per_query:
        p_str = f"{precision:.0%}" if precision is not None else "N/A"
        extra = f"  미라벨:{unlabeled_ids}" if unlabeled_ids else ""
        print(f"{qid:<6}{n:>6}{labeled:>8}{relevant:>10}{p_str:>12}{extra}")

    print()
    print(f"top-15 라벨 커버리지: {total_labeled}/{total_labeled + total_unlabeled}건")
    if total_labeled:
        print(f"production 경로 전체 precision@15: {total_relevant / total_labeled:.1%}")


if __name__ == "__main__":
    asyncio.run(main())
