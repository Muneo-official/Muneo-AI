from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    anthropic_api_key: str = ""
    mongo_uri: str = ""
    mongo_db_name: str = "estimate_db"

    embed_model: str = "paraphrase-multilingual-MiniLM-L12-v2"
    vector_index_name: str = "estimate_vector_index"

    reranker_model: str = "Dongjin-kr/ko-reranker"
    vector_candidate_pool: int = 40  # 벡터 검색 후보 풀 크기 (리랭킹 전, TOP_K보다 넉넉하게)

    # Mongo 커넥션 — 명시적으로 안 잡아두면 기본값(maxPoolSize=100)이라 워커 여러 개 띄울 때
    # Atlas M0의 낮은 동시 커넥션 한도를 쉽게 넘길 수 있다. 워커 1개 기준으로 보수적으로 잡음
    # (워커를 늘리면 이 값을 워커 수에 맞게 낮춰야 함 — 총합이 M0 한도를 넘지 않게).
    mongo_max_pool_size: int = 20
    mongo_server_selection_timeout_ms: int = 5000


@lru_cache
def get_settings() -> Settings:
    return Settings()
