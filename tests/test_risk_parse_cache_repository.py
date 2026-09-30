import pytest

from app.repositories.risk_parse_cache_repository import CachedParse, RiskParseCacheRepository


class _Cursor:
    def __init__(self, docs):
        self._docs = docs

    async def to_list(self, length):
        return self._docs[:length]


class _FakeCollection:
    """repository가 쓰는 find/update_many/update_one($set·$setOnInsert·upsert)만 흉내 내는 인메모리 컬렉션."""

    def __init__(self):
        self.docs: dict[str, dict] = {}

    def find(self, query):
        ids = query["_id"]["$in"]
        return _Cursor([dict(self.docs[i]) for i in ids if i in self.docs])

    async def update_many(self, query, update):
        for i in query["_id"]["$in"]:
            if i in self.docs:
                self.docs[i].update(update["$set"])

    async def update_one(self, query, update, upsert=False):
        key = query["_id"]
        if key not in self.docs:
            assert upsert
            self.docs[key] = {"_id": key, **update.get("$setOnInsert", {})}
        self.docs[key].update(update.get("$set", {}))


def _parsed(desc: str) -> CachedParse:
    return CachedParse(line_items=[{"category": "도배", "description": desc, "amount": 1}], input_tokens=10, output_tokens=5)


@pytest.mark.asyncio
async def test_get_many_returns_only_existing_keys():
    repo = RiskParseCacheRepository(_FakeCollection())
    await repo.put("a:v1", _parsed("A"))

    found = await repo.get_many(["a:v1", "b:v1"])

    assert list(found) == ["a:v1"]
    assert found["a:v1"] == _parsed("A")


@pytest.mark.asyncio
async def test_put_does_not_overwrite_existing_result():
    # 같은 이미지가 동시에 두 번 파싱돼도 처음 저장된 결과가 계속 나가야 한다(일관성)
    repo = RiskParseCacheRepository(_FakeCollection())
    await repo.put("a:v1", _parsed("first"))
    await repo.put("a:v1", _parsed("second"))

    found = await repo.get_many(["a:v1"])

    assert found["a:v1"].line_items[0]["description"] == "first"


@pytest.mark.asyncio
async def test_get_many_refreshes_last_used_at():
    collection = _FakeCollection()
    repo = RiskParseCacheRepository(collection)
    await repo.put("a:v1", _parsed("A"))
    stored_at = collection.docs["a:v1"]["last_used_at"]
    created_at = collection.docs["a:v1"]["created_at"]

    await repo.get_many(["a:v1"])

    assert collection.docs["a:v1"]["last_used_at"] >= stored_at
    assert collection.docs["a:v1"]["created_at"] == created_at


@pytest.mark.asyncio
async def test_get_many_skips_update_when_nothing_found():
    collection = _FakeCollection()
    repo = RiskParseCacheRepository(collection)

    assert await repo.get_many(["missing:v1"]) == {}
    assert collection.docs == {}
