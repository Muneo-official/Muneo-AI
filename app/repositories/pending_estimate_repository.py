import uuid
from datetime import UTC, datetime, timedelta

from motor.motor_asyncio import AsyncIOMotorCollection

# /generate가 계산한 결과를 짧게 들고 있다가 /save에서 그대로 꺼내 쓰기 위한 TTL. 클라이언트가
# 견적을 확인하고 저장 버튼을 누르기까지 여유 있게, 하지만 무한정 남겨두지 않게 15분으로 잡음.
PENDING_TTL_MINUTES = 15


class PendingEstimateRepository:
    """`pending_estimates` 컬렉션 접근 계층.

    /estimates/save가 클라이언트로부터 input/result를 그대로 받아 저장하면, 클라이언트가 실제로
    계산된 적 없는 값(예: 조작된 총_견적_범위)을 저장할 수 있다 — 이러면 estimate_feedback 기반
    정확도 집계(scripts/accuracy_report.py)도 조작 가능해진다.

    그래서 /generate 시점에 서버가 계산한 (input, result)를 여기에 잠깐 캐싱해두고 토큰만
    발급한다. /save는 그 토큰으로 캐시된 값을 그대로 꺼내 쓰고, 클라이언트가 보낸 값은 신뢰하지
    않는다. TTL 인덱스(expires_at)로 자동 정리되고, 조회 즉시 삭제(1회용)해서 재사용도 막는다.
    """

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def create(self, user_id: str, input_data: dict, result: dict) -> str:
        token = str(uuid.uuid4())
        now = datetime.now(UTC)
        await self._collection.insert_one({
            "_id": token,
            "user_id": user_id,
            "input": input_data,
            "result": result,
            "created_at": now,
            "expires_at": now + timedelta(minutes=PENDING_TTL_MINUTES),
        })
        return token

    async def consume(self, token: str, user_id: str) -> tuple[dict, dict] | None:
        """토큰에 해당하는 (input, result)를 반환하고 즉시 삭제한다. user_id가 다르거나
        만료(TTL로 이미 삭제)됐으면 None."""
        doc = await self._collection.find_one_and_delete({"_id": token, "user_id": user_id})
        if doc is None:
            return None
        return doc["input"], doc["result"]
