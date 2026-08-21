# MuneoAI

인테리어 리모델링 가견적 시스템. 실제 시공 사례(크롤링 데이터) 통계 기반으로 가견적을 산출한다 —
**가견적 "숫자" 산출에는 LLM을 쓰지 않는다** (신뢰성과 가격 할루시네이션 방지를 위한 의도적 설계 결정).

## 아키텍처

```
app/
├── api/routers/       # HTTP 요청/응답 (얇게 유지)
├── domain/             # 비즈니스 로직 — DB 클라이언트를 모른다
├── repositories/       # DB 접근 캡슐화 ($vectorSearch 등)
├── schemas/            # Pydantic 요청/응답 모델
└── core/               # 설정, DI(lifespan), 로깅, rate limit
```

- **DB**: MongoDB Atlas 하나로 통합 (Vector Search로 벡터 검색까지 처리, Chroma 없음)
- **검색**: `$vectorSearch` 필터 기반 점진적 폴백(Stage 1~5) → BM25+RRF 하이브리드 →
  (선택) Cross-encoder (`Dongjin-kr/ko-reranker`) 리랭킹 — `USE_RERANKER`로 켜고 끈다
- **보정계수**: `correction_coefficients` 컬렉션에서 버전 관리 (앱 시작 시 1회 로드)
- **LLM**: 이 프로젝트 어디에도 가견적 숫자 산출에 관여하지 않음. 향후 자연어 요약/자유입력
  파싱 등 보조 역할로만 확장 예정
