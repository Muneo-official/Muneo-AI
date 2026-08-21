from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorCollection


class CoefficientRepository:
    """`correction_coefficients` 컬렉션 접근 계층.

    보정계수(자재등급/건물연식/지역/거주여부/공사시기)를 estimate_engine.py에 하드코딩하는 대신
    버전 관리한다 — 튜닝할 때마다 배포하지 않아도 되고, 과거 견적이 어떤 계수로 산출됐는지
    (estimates.coefficient_version) 역추적할 수 있다.
    """

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def get_active(self, as_of: datetime | None = None) -> dict:
        """effective_from이 as_of(기본: 지금) 이전인 것 중 가장 최신 버전을 반환한다."""
        as_of = as_of or datetime.now(UTC)
        doc = await self._collection.find_one(
            {"effective_from": {"$lte": as_of}},
            sort=[("effective_from", -1)],
        )
        if doc is None:
            raise RuntimeError(
                "correction_coefficients 컬렉션이 비어 있습니다. "
                "먼저 scripts/seed_coefficients.py를 실행하세요."
            )
        return doc
