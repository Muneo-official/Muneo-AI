from datetime import UTC, datetime, timedelta

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection

# 자재/인건비 단가가 이 기간 내에는 크게 안 바뀐다고 가정 — engine의 "공사시기" 옵션 중
# 가장 짧은 구간(1개월이내)보다 넉넉하게, 하지만 3개월치 견적을 그대로 방치하진 않게 30일로 설정.
VALID_DAYS = 30


class EstimateRepository:
    """`estimates` 컬렉션 접근 계층 (저장된 견적 CRUD)."""

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def save(self, user_id: str, input_data: dict, result: dict) -> str:
        created_at = datetime.now(UTC)
        doc = {
            "user_id": user_id,
            "created_at": created_at,
            "input": input_data,
            "result": result,
            "status": "saved",
            "valid_until": created_at + timedelta(days=VALID_DAYS),
        }
        res = await self._collection.insert_one(doc)
        return str(res.inserted_id)

    async def mark_contracted(self, estimate_id: str, user_id: str) -> bool:
        try:
            oid = ObjectId(estimate_id)
        except InvalidId:
            return False
        res = await self._collection.update_one(
            {"_id": oid, "user_id": user_id},
            {"$set": {"status": "contracted"}},
        )
        return res.modified_count > 0

    async def list_by_user(self, user_id: str, limit: int = 100) -> list[dict]:
        cursor = self._collection.find(
            {"user_id": user_id},
            # 목록 조회에서는 무거운/부가 필드는 뺀다 (result 하위 필드라 경로를 명시해야 실제로 빠짐)
            {"result.참고_사례": 0, "result.참고_사례_수": 0, "result.검색_쿼리": 0},
        )
        docs = await cursor.to_list(length=limit)
        for doc in docs:
            doc["id"] = str(doc.pop("_id"))
        return docs

    async def delete(self, estimate_id: str, user_id: str) -> bool | None:
        try:
            oid = ObjectId(estimate_id)
        except InvalidId:
            return None
        res = await self._collection.delete_one({"_id": oid, "user_id": user_id})
        return res.deleted_count > 0
