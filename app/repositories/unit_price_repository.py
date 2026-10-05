from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorCollection

from app.domain.unit_price_reference import Key, QuantityKey


class UnitPriceRepository:
    """`unit_price_reference` 컬렉션 접근 계층 — 리스크 진단이 품목의 단가와 수량을 견주는 기준표.

    문서 하나가 품목 하나다.
      단가: {kind: "unit_price", category, name, n, p10, median, p90, built_at}
      수량: {kind: "quantity", category, name, unit, n, median, p90, built_at} — 평당 수량
    표는 scripts/build_unit_price_reference.py가 estimate_cases에서 통째로 다시 만들어 넣고, 서버는 시작할 때 한 번 읽는다.
    """

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def load(self) -> tuple[dict[Key, dict[str, int]], dict[QuantityKey, dict[str, float]]]:
        """반환: (단가 표, 수량 표)."""
        prices, quantities = {}, {}
        async for doc in self._collection.find({}, {"_id": 0}):
            if doc.get("kind") == "quantity":
                quantities[(doc["category"], doc["name"], doc["unit"])] = {k: doc[k] for k in ("n", "median", "p90")}
            else:
                prices[(doc["category"], doc["name"])] = {k: doc[k] for k in ("n", "p10", "median", "p90")}
        return prices, quantities

    async def replace_all(self, prices: dict[Key, dict[str, int]], quantities: dict[QuantityKey, dict[str, float]]) -> int:
        """표를 통째로 바꾼다. 반환: 넣은 문서 수."""
        built_at = datetime.now(UTC)
        docs = [{"kind": "unit_price", "category": c, "name": n, **stats, "built_at": built_at} for (c, n), stats in prices.items()]
        docs += [{"kind": "quantity", "category": c, "name": n, "unit": u, **stats, "built_at": built_at}
                 for (c, n, u), stats in quantities.items()]
        await self._collection.delete_many({})
        if docs:
            await self._collection.insert_many(docs)
        return len(docs)
