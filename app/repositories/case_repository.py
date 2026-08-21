from motor.motor_asyncio import AsyncIOMotorCollection

from app.core.config import Settings


class CaseRepository:
    """`estimate_cases` 컬렉션 접근 계층.

    Atlas Vector Search($vectorSearch) 집계 파이프라인을 이 클래스 안에 캡슐화해서,
    도메인 레이어(app/domain/estimate_engine.py)가 Mongo 쿼리 문법을 몰라도 되게 한다.
    """

    def __init__(self, collection: AsyncIOMotorCollection, settings: Settings):
        self._collection = collection
        self._index_name = settings.vector_index_name

    async def vector_search(
        self,
        query_embedding: list[float],
        mongo_filter: dict | None,
        limit: int,
        num_candidates: int = 150,
    ) -> list[dict]:
        vector_search_stage: dict = {
            "index": self._index_name,
            "path": "embedding",
            "queryVector": query_embedding,
            "numCandidates": num_candidates,
            "limit": limit,
        }
        if mongo_filter:
            vector_search_stage["filter"] = mongo_filter

        pipeline = [
            {"$vectorSearch": vector_search_stage},
            {"$project": {"embedding": 0}},
        ]
        cursor = self._collection.aggregate(pipeline)
        return [doc async for doc in cursor]

    async def find_by_article_ids(self, article_ids: list[str]) -> dict[str, dict]:
        if not article_ids:
            return {}
        cursor = self._collection.find(
            {"article_id": {"$in": article_ids}},
            {"parsed_estimate": 1, "article_id": 1, "_id": 0},
        )
        return {doc["article_id"]: doc async for doc in cursor}

    async def count(self) -> int:
        return await self._collection.estimated_document_count()
