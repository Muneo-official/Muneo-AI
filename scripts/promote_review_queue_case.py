"""review_queue의 사례 하나를 사람이 검토·수정한 뒤 estimate_cases로 승격한다.

실행:
    python -m scripts.promote_review_queue_case <article_id>
"""

import argparse
import asyncio

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings

load_dotenv()


async def main(article_id: str) -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]
    queue_col = db["review_queue"]
    cases_col = db["estimate_cases"]

    case = await queue_col.find_one({"article_id": article_id})
    if not case:
        print(f"[ERR] review_queue에서 article_id={article_id}를 찾을 수 없음")
        client.close()
        return

    case.pop("_validation", None)  # 승격 시 검증 메타데이터는 정리 — estimate_cases는 정상 데이터 컬렉션
    await cases_col.insert_one(case)
    await queue_col.delete_one({"_id": case["_id"]})
    print(f"[OK] article_id={article_id} review_queue -> estimate_cases 승격 완료")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("article_id", type=str)
    args = parser.parse_args()
    asyncio.run(main(args.article_id))
