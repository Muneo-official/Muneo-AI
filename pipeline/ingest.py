"""엔드투엔드 진입점 — 크롤러가 저장한 폴더 하나를 받아 파싱→검증→라우팅→저장까지.

지금까지 pipeline/에 따로 만든 조각(crawl_filter, image_prep, vision_client, parsing,
aggregation, routing)을 전부 이어붙인다. 기존에는 pipeline/reference/parse_estimates.py
(파싱) + scripts/migrate_to_mongo.py(Mongo 적재)를 사람이 순서대로 따로 실행해야 했는데,
이 모듈의 `ingest_article()` 하나로 끝난다.

크롤러(pipeline/reference/crawler.py)가 저장하는 구조를 그대로 가정한다:
    {article_dir}/{article_id}.json  — article_id, region, size_pyeong, request_body_text,
                                        local_images 등을 담은 원본 레코드
    {article_dir}/{article_id}_N.{ext}  — local_images가 가리키는 실제 이미지 파일들
"""

import json
import pathlib

from motor.motor_asyncio import AsyncIOMotorCollection

from pipeline.aggregation import build_category_costs, build_has_flags
from pipeline.parsing import merge_and_validate
from pipeline.routing import route_case
from pipeline.vision_client import parse_image


def load_article_record(article_dir: str) -> dict:
    """크롤러가 저장한 {article_id}.json을 읽는다."""
    dir_path = pathlib.Path(article_dir)
    json_path = dir_path / f"{dir_path.name}.json"
    return json.loads(json_path.read_text(encoding="utf-8"))


def process_article(article_dir: str) -> dict:
    """폴더 하나를 읽어 파싱·검증·집계까지 끝낸 최종 레코드를 반환한다 (DB 저장은 안 함).

    반환 레코드는 app/domain/estimate_engine.py가 실제로 읽는 필드(has_*, cost_*,
    total_cost, cost_per_pyeong, parsed_estimate)를 그대로 담는다 — 파싱만 하고 이
    필드들을 안 채우면 저장은 되지만 견적 엔진이 참고 사례로 못 쓴다.
    """
    record = load_article_record(article_dir)
    image_paths = record.get("local_images") or []
    per_image_results = [parse_image(p) for p in image_paths]

    size_pyeong = int(record.get("size_pyeong") or 0)
    validated = merge_and_validate(per_image_results, size_pyeong)

    if not validated:
        record["parsed_estimate"] = {}
        record["_validation"] = None
        return record

    validation = validated.pop("_validation", None)
    line_items = validated.get("line_items", [])
    total_cost = int(validated.get("total_cost") or 0)

    record["parsed_estimate"] = validated
    record["_validation"] = validation
    record["total_cost"] = total_cost
    record["cost_per_pyeong"] = int(total_cost / size_pyeong) if size_pyeong > 0 else 0
    record.update(build_category_costs(line_items, total_cost))
    record.update(build_has_flags(record.get("request_body_text") or "", line_items))
    return record


async def ingest_article(
    article_dir: str,
    cases_col: AsyncIOMotorCollection,
    queue_col: AsyncIOMotorCollection,
) -> str:
    """process_article() 결과를 신뢰도에 따라 estimate_cases/review_queue에 저장한다.

    parsed_estimate가 아예 없으면(모든 이미지가 견적서가 아니었거나 보일러플레이트만
    있었던 경우) 무조건 review_queue로 보낸다 — confidence 계산 자체가 안 되기 때문.

    반환값: 실제로 저장된 컬렉션 이름("estimate_cases" 또는 "review_queue").
    """
    record = process_article(article_dir)
    validation = record.get("_validation")

    if not record.get("parsed_estimate"):
        destination = "review_queue"
    else:
        destination = route_case(validation["confidence"] if validation else 0.0)

    record["article_id"] = str(record.get("article_id", pathlib.Path(article_dir).name))
    col = cases_col if destination == "estimate_cases" else queue_col
    await col.update_one({"article_id": record["article_id"]}, {"$set": record}, upsert=True)
    return destination
