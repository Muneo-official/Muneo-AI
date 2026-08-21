# MuneoAI

인테리어 리모델링 가견적 시스템. 실제 시공 사례(크롤링 데이터) 통계 기반으로 가견적을 산출한다 —
**가견적 "숫자" 산출에는 LLM을 쓰지 않는다** (신뢰성과 가격 할루시네이션 방지를 위한 의도적 설계 결정).

기존 프로젝트(Chroma + Mongo 이중 DB, sys.path 해킹, 전역 싱글톤)를 FastAPI 실무 구조로 재설계한
저장소다. 각 결정의 배경과 이유는 [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md)에
자세히 기록되어 있다.

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
- **검색**: `$vectorSearch` 필터 기반 점진적 폴백(Stage 1~5) → BM25+RRF 하이브리드 → Cross-encoder
  (`Dongjin-kr/ko-reranker`) 리랭킹
- **보정계수**: `correction_coefficients` 컬렉션에서 버전 관리 (앱 시작 시 1회 로드)
- **LLM**: 이 프로젝트 어디에도 가견적 숫자 산출에 관여하지 않음. 향후 자연어 요약/자유입력
  파싱 등 보조 역할로만 확장 예정

자세한 구조와 각 단계별 구현 배경은 [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md) 참고.

## 시작하기

### 요구사항
- Python 3.10+ (개발은 3.14 기준)
- MongoDB Atlas 클러스터 (Vector Search Index 설정 필요 — 아래 참고)

### 환경변수 (`.env`)
```env
ANTHROPIC_API_KEY=   # 현재는 가견적 숫자 산출에 미사용, 향후 보조 기능용
MONGO_URI=           # Atlas 연결 문자열
```
그 외 설정(임베딩/리랭커 모델명, 커넥션 풀 크기 등)은 `app/core/config.py`에 기본값이 있어
필수는 아니다.

### 로컬 실행
```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```
`http://localhost:8000/docs`에서 Swagger 문서 확인 가능.

### Docker
```bash
docker build -t muneoai .
docker run -p 8000:8000 --env-file .env muneoai
```
첫 실행 시 임베딩/리랭커 모델(~750MB)을 컨테이너 안에서 다운로드하므로 기동까지 1~2분 걸릴 수
있다. `docker-compose.yml`은 로컬 Mongo 컨테이너도 같이 띄우지만, 실제로는 `.env`의 `MONGO_URI`가
Atlas를 가리키므로 로컬 Mongo는 개발 편의용(선택)이다.

### 테스트 / 린트
```bash
pytest
ruff check app tests scripts eval
```

## API

| 엔드포인트 | 설명 |
|---|---|
| `POST /estimates/generate` | 가견적 산출 (저장 안 함). 응답에 `estimate_token`(1회용, TTL 15분) 포함. `x-user-id`별 10/분 + 전역 100/분 제한 |
| `POST /estimates/save` | `estimate_token`으로 저장 — `/generate`가 서버에서 계산한 값을 그대로 저장하며, 클라이언트가 값을 조작할 수 없다 |
| `GET /estimates` | 저장된 견적 목록 |
| `DELETE /estimates/{id}` | 견적 삭제 |
| `POST /estimates/{id}/feedback` | 실제 계약금액 기록 (정확도 피드백 루프) |
| `GET /health` | 헬스체크 |

모든 엔드포인트(`/health` 제외)는 `x-user-id` 헤더가 필요하다 — 이 서버는 자체 인증이 없고,
프록시(Spring 등)가 사용자를 식별해서 헤더로 넘겨주는 것을 전제로 한다.

**`/estimates/save`가 `{input, result}`를 직접 받지 않는 이유**: 클라이언트가 `result`를 직접
보내면 `총_견적_범위` 같은 값을 조작해서 저장할 수 있고, 이는 `estimate_feedback` 기반 정확도
집계(`scripts/accuracy_report.py`)까지 조작 가능하게 만든다. `/generate`가 계산한 값을 서버가
`pending_estimates`에 15분 TTL로 캐싱해두고 1회용 토큰만 발급하는 식으로 막는다.

## 운영 스크립트 (`scripts/`)

일회성 마이그레이션과 반복 실행용 운영 스크립트가 섞여 있다:

| 스크립트 | 용도 | 실행 빈도 |
|---|---|---|
| `import_seed_data.py` | 초기 정형 데이터 이관 | 1회 |
| `backfill_embeddings.py` | Chroma → Mongo 임베딩/메타데이터 백필 | 1회 |
| `setup_indexes.py` | 모든 인덱스 생성 (멱등) | 스키마 바뀔 때마다 |
| `seed_coefficients.py --version X.Y.Z` | 보정계수 새 버전 시드 | 계수 튜닝할 때마다 |
| `accuracy_report.py` | 실제 계약금액 vs 예측 범위 MAPE/coverage 집계 | 주기적으로 (GitHub Actions로 매주 자동 실행) |
| `expire_estimates.py` | `valid_until` 지난 `status=saved` 견적을 `expired`로 전환 | 주기적으로 (cron 권장) |
| `log_report.py` | 구조화 로그(`logs/app.log`) 요약 | 필요할 때 |

## Retrieval 평가 (`eval/`)

벡터 검색만 썼을 때 vs 하이브리드(BM25+RRF)+cross-encoder 리랭킹을 사람이 라벨링한 ground truth로
비교하는 정량 평가. 방법론과 결과는 [`docs/IMPLEMENTATION_LOG.md`](docs/IMPLEMENTATION_LOG.md) 2-7,
라벨링 절차는 [`eval/LABELING_GUIDE.md`](docs/LABELING_GUIDE.md) 참고.

```bash
python -m eval.sample_queries      # 쿼리 샘플링
python -m eval.build_pool          # 라벨링 대상 pool 생성
python -m eval.flag_review_rows    # 검토 필요한 행만 추출
# (사람이 eval/test_inputs/review_priority.csv 라벨링)
python -m eval.apply_review_labels # 라벨 병합
python -m eval.retrieval_eval      # precision@k / recall 비교
```
