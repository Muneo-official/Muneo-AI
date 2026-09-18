"""production_rerank_ablation_eval의 "Stage 필터 + 벡터 단독" 쪽 top-15 중 labels.csv에
없는 (query_id, article_id)만 추려 라벨링용 CSV로 낸다 — 하이브리드+리랭킹과 공정하게
비교하려면 두 경로가 똑같이 전량 라벨 커버리지를 가져야 한다.

실행: python -m eval.label_rerank_ablation_candidates
이후: eval/test_inputs/rerank_ablation_review.csv 라벨링 -> eval.apply_production_labels
      (같은 형식이라 그 스크립트를 그대로 재사용 — REVIEW_PATH만 다르면 되므로 여기서
      바로 labels.csv에 반영까지 한다)
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
from eval.build_pool import _case_works, _suggested_relevant
from eval.production_rerank_ablation_eval import resolve_filtered_candidates

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"
OUT_PATH = pathlib.Path(__file__).parent / "test_inputs" / "rerank_ablation_review.csv"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]
    repo = CaseRepository(col, settings)

    embedder = SentenceTransformer(settings.embed_model)
    reranker = CrossEncoder(settings.reranker_model, max_length=512)
    engine = EstimateEngine(case_repository=repo, embedder=embedder, reranker=reranker)

    queries = json.loads(QUERIES_PATH.read_text(encoding="utf-8"))

    existing_labels: set[tuple[str, str]] = set()
    with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            existing_labels.add((row["query_id"], row["article_id"]))

    total = await repo.count()
    pool_n = min(engine._vector_candidate_pool, total) if total else engine._vector_candidate_pool

    rows = []
    for q in queries:
        qid = q["query_id"]
        inp = q["input"]
        _, cases = await resolve_filtered_candidates(engine, repo, inp, pool_n)

        for c in cases[:15]:
            aid = str(c.get("article_id"))
            if (qid, aid) in existing_labels:
                continue
            suggested = _suggested_relevant(inp, c)
            rows.append({
                "query_id": qid,
                "query_size": inp.get("평수"),
                "query_region": inp.get("지역"),
                "query_works": ",".join(inp.get("공종", [])),
                "article_id": aid,
                "region": c.get("region"),
                "size_pyeong": c.get("size_pyeong"),
                "works": ",".join(sorted(_case_works(c))),
                "suggested_relevant": int(suggested),
                "label": int(suggested),
            })
            existing_labels.add((qid, aid))  # 같은 배치 안 중복 방지

    client.close()

    with OUT_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "query_id", "query_size", "query_region", "query_works",
            "article_id", "region", "size_pyeong", "works",
            "suggested_relevant", "label",
        ])
        writer.writeheader()
        writer.writerows(rows)

    print(f"[OK] 신규(라벨 없음) 후보 {len(rows)}건 -> {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
