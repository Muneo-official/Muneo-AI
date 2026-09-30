from dataclasses import dataclass
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorCollection

# 마지막으로 쓰인 지 이만큼 지난 캐시는 TTL 인덱스(last_used_at, scripts/setup_indexes.py)가 지운다.
# 캐시가 걸릴 때마다 last_used_at을 갱신하므로, 계속 다시 올라오는 이미지는 만료되지 않고 같은 결과를 유지한다.
# (이미 만든 TTL 인덱스의 기간은 create_index를 다시 돌려도 안 바뀐다 — 바꾸려면 collMod로 수정)
PARSE_CACHE_TTL_DAYS = 90


@dataclass
class CachedParse:
    line_items: list[dict]
    # 이 결과를 처음 만들 때 든 토큰 — 캐시가 걸린 요청에서 "아낀 비용"을 로그로 남기는 데 쓴다
    input_tokens: int
    output_tokens: int


class RiskParseCacheRepository:
    """`risk_parse_cache` 컬렉션 접근 계층 — 리스크 진단 이미지 1장의 Vision 파싱 결과 캐시.

    키는 "원본 업로드 바이트의 sha256:파싱 버전(pipeline.vision_client.RISK_PARSE_VERSION)"이다.
    같은 이미지를 다시 올리면 Vision API를 다시 부르지 않고 예전 파싱 결과를 그대로 쓴다 — 비용·응답시간을
    아끼는 것과 함께, 모델 출력의 실행 간 변동 없이 같은 이미지는 항상 같은 결과를 받게 하는 게 목적이다.

    최종 리포트가 아니라 파싱 결과만 저장한다. 룰 분석·고층 양중비·가격 이상 탐지는 폼 입력(층수·지역·평수)에
    따라 달라지므로 매번 새로 돌린다. 이미지 원본은 저장하지 않는다.
    """

    def __init__(self, collection: AsyncIOMotorCollection):
        self._collection = collection

    async def get_many(self, keys: list[str]) -> dict[str, CachedParse]:
        """있는 것만 반환하고, 찾은 문서의 last_used_at을 갱신해 TTL을 연장한다."""
        docs = await self._collection.find({"_id": {"$in": keys}}).to_list(length=len(keys))
        if docs:
            await self._collection.update_many(
                {"_id": {"$in": [doc["_id"] for doc in docs]}},
                {"$set": {"last_used_at": datetime.now(UTC)}},
            )
        return {
            doc["_id"]: CachedParse(
                line_items=doc["line_items"],
                input_tokens=doc.get("input_tokens", 0),
                output_tokens=doc.get("output_tokens", 0),
            )
            for doc in docs
        }

    async def put(self, key: str, parsed: CachedParse) -> None:
        """이미 있으면 파싱 결과를 덮어쓰지 않는다($setOnInsert) — 같은 이미지가 동시에 두 번 파싱돼도
        처음 저장된 결과가 계속 쓰이도록."""
        now = datetime.now(UTC)
        await self._collection.update_one(
            {"_id": key},
            {
                "$setOnInsert": {
                    "line_items": parsed.line_items,
                    "input_tokens": parsed.input_tokens,
                    "output_tokens": parsed.output_tokens,
                    "created_at": now,
                },
                "$set": {"last_used_at": now},
            },
            upsert=True,
        )
