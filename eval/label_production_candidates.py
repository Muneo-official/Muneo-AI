"""EstimateEngine.retrieve_cases()(Stage 필터 포함, 실제 production 경로)가 반환하는
후보 중 기존 labels.csv에 라벨이 없는 (query_id, article_id) 쌍만 추려 라벨링용 CSV로
낸다.

기존 eval/refresh_pool.py + flag_review_rows.py는 "필터 없는 벡터 검색 top-20"만
보고 pool을 만들어서, Stage 필터를 거친 candidate는 애초에 그 pool에 없던 경우가
많다(실측: 24개 쿼리 top-15 314건 중 204건이 기존 라벨에 전혀 없었음). 이 스크립트는
그 신규분만 별도로 라벨링 대상으로 뽑는다 — 기존 pool.json은 안 건드린다.

실행:
    python -m eval.label_production_candidates
이후:
    eval/test_inputs/production_review.csv를 열어 label 열(0/1)을 채운 뒤
    python -m eval.apply_production_labels
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

load_dotenv()

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"
OUT_PATH = pathlib.Path(__file__).parent / "test_inputs" / "production_review.csv"


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
    if LABELS_CSV_PATH.exists():
        with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                existing_labels.add((row["query_id"], row["article_id"]))

    rows = []
    for q in queries:
        qid = q["query_id"]
        inp = q["input"]
        query_text = engine.build_query(inp)
        cases = await engine.retrieve_cases(query_text, inp)

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
