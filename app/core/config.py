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

    # cross-encoder 리랭킹 사용 여부. False면 CrossEncoder를 아예 로드하지 않고
    # 하이브리드(벡터+BM25/RRF) 결과 상위 TOP_K를 그대로 쓴다.
    #
    # 기본값이 False인 이유 — 리랭커(Dongjin-kr/ko-reranker)는 XLM-R large 계열이라 fp32
    # 가중치만 약 2.2GB로 이 서비스 메모리의 대부분을 차지하는데, 정량 평가 결과가
    # precision@10 +2.9%p / precision@5 -3.3%p로 혼재돼 있다(docs/IMPLEMENTATION_LOG.md 2-7).
    # 게다가 리랭커가 매긴 "순서"는 최종 출력에 반영되지 않는다: 금액은 cost_range()의
    # IQR 집계(순서 무관)로 나오고, 응답의 참고_사례는 평수 차이 기준으로 재정렬된다.
    # 즉 리랭커가 실제로 바꾸는 건 RRF 상위 RERANK_POOL건 중 어느 것이 TOP_K에 남는지뿐이라,
    # 비용 대비 이득이 확인되지 않았다. 켜고 끄며 비교할 수 있게 스위치로 남겨둔다.
    use_reranker: bool = False

    # Mongo 커넥션 — 명시적으로 안 잡아두면 기본값(maxPoolSize=100)이라 워커 여러 개 띄울 때
    # Atlas M0의 낮은 동시 커넥션 한도를 쉽게 넘길 수 있다. 워커 1개 기준으로 보수적으로 잡음
    # (워커를 늘리면 이 값을 워커 수에 맞게 낮춰야 함 — 총합이 M0 한도를 넘지 않게).
    mongo_max_pool_size: int = 20
    mongo_server_selection_timeout_ms: int = 5000


@lru_cache
def get_settings() -> Settings:
    return Settings()
