from datetime import UTC, datetime

from bson import ObjectId
from bson.errors import InvalidId
from motor.motor_asyncio import AsyncIOMotorCollection


class RiskReportRepository:
    """`risk_reports` 컬렉션 접근 계층 (리스크 진단 리포트 CRUD)."""

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def save(self, user_id: str, user_input: dict, result: dict) -> str:
        doc = {
            "user_id": user_id,
            "created_at": datetime.now(UTC),
            "input": user_input,
            "result": result,
        }
        res = await self._collection.insert_one(doc)
        return str(res.inserted_id)

    async def list_by_user(self, user_id: str, limit: int = 100) -> list[dict]:
        cursor = self._collection.find({"user_id": user_id})
        docs = await cursor.to_list(length=limit)
        for doc in docs:
            doc["id"] = str(doc.pop("_id"))
        return docs

    async def delete(self, report_id: str, user_id: str) -> bool | None:
        try:
            oid = ObjectId(report_id)
        except InvalidId:
            return None
        res = await self._collection.delete_one({"_id": oid, "user_id": user_id})
        return res.deleted_count > 0
