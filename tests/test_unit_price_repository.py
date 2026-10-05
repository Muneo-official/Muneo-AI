from app.domain.unit_price_reference import UnitPriceReference
from app.repositories.unit_price_repository import UnitPriceRepository


class _FakeCollection:
    """repository가 쓰는 find/delete_many/insert_many만 흉내 내는 인메모리 컬렉션."""

    def __init__(self):
        self.docs: list[dict] = []

    async def _iterate(self, projection):
        for doc in self.docs:
            yield {k: v for k, v in doc.items() if projection.get(k, 1)}

    def find(self, query, projection):
        return self._iterate(projection)

    async def delete_many(self, query):
        self.docs = []

    async def insert_many(self, docs):
        self.docs.extend({"_id": len(self.docs) + i, **doc} for i, doc in enumerate(docs))


PRICES = {
    ("도배", "부자재", "식"): {"n": 30, "p10": 120_000, "median": 250_000, "p90": 400_000},
    ("도배", "부자재", "m2"): {"n": 20, "p10": 1_500, "median": 1_500, "p90": 2_000},
    ("도배", "인건비", "인"): {"n": 40, "p10": 260_000, "median": 280_000, "p90": 300_000},
    ("도배", "인건비", ""): {"n": 40, "p10": 260_000, "median": 280_000, "p90": 300_000},
}
QUANTITIES = {("도배", "실크벽지", "평"): {"n": 25, "median": 2.9, "p90": 3.75}}


async def test_넣은_표를_단위까지_그대로_읽는다():
    # 같은 품목의 단위별 기준이 읽을 때 하나로 합쳐지면 단위를 나눈 의미가 없다
    repo = UnitPriceRepository(_FakeCollection())

    assert await repo.replace_all(PRICES, QUANTITIES) == 5
    assert await repo.load() == (PRICES, QUANTITIES)


async def test_다시_넣으면_앞의_표는_남지_않는다():
    repo = UnitPriceRepository(_FakeCollection())
    await repo.replace_all(PRICES, QUANTITIES)

    await repo.replace_all({("도배", "인건비", "인"): PRICES[("도배", "인건비", "인")]}, {})

    assert await repo.load() == ({("도배", "인건비", "인"): PRICES[("도배", "인건비", "인")]}, {})


async def test_읽은_표로_단위에_맞는_기준과_견준다():
    repo = UnitPriceRepository(_FakeCollection())
    await repo.replace_all(PRICES, QUANTITIES)
    ref = UnitPriceReference(*await repo.load())

    per_m2 = {"category": "도배", "description": "부자재(풀)", "unit_price": 5_000, "amount": 500_000, "unit": "㎡"}
    lump = {"category": "도배", "description": "부자재(풀)", "unit_price": 250_000, "amount": 250_000, "unit": "식"}
    assert ref.judge(per_m2)[0] == "높음" and ref.judge(lump)[0] is None
