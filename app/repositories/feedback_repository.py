from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorCollection


class FeedbackRepository:
    """`estimate_feedback` 컬렉션 접근 계층.

    실제 계약금액을 기록해서, 나중에 (실제 계약금액 vs 견적 범위)를 비교하는 온라인 정확도
    측정(폐루프)을 가능하게 한다. 지금은 기록만 하고, 집계/분석은 범위 밖 — 그건 이 데이터가
    어느 정도 쌓인 뒤에 별도로 할 일이다.
    """

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def create(self, estimate_id: str, actual_cost: int, contracted_at: datetime) -> str:
        doc = {
            "estimate_id": estimate_id,
            "actual_cost": actual_cost,
            "contracted_at": contracted_at,
            "created_at": datetime.now(UTC),
        }
        res = await self._collection.insert_one(doc)
        return str(res.inserted_id)
