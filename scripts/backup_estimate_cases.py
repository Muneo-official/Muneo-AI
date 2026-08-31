"""estimate_cases 컬렉션 전체를 로컬 JSON으로 백업한다.

scripts/import_seed_data.py가 읽을 수 있는 것과 동일한 형태(JSON 배열)로 저장 —
문제가 생기면 그 스크립트로 복구할 수 있다. _id는 재적재 시 충돌을 피하기 위해 제외한다.

실행: python -m scripts.backup_estimate_cases
"""

import asyncio
import pathlib
from datetime import UTC, datetime

from bson import json_util
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings

load_dotenv()

BACKUP_DIR = pathlib.Path(__file__).parent.parent / "backups"


async def main() -> None:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    docs = []
    async for doc in col.find({}):
        doc.pop("_id", None)
        docs.append(doc)

    client.close()

    BACKUP_DIR.mkdir(exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = BACKUP_DIR / f"estimate_cases_{timestamp}.json"
    out_path.write_text(json_util.dumps(docs, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] {len(docs)}건 백업 완료 -> {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
