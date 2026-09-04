# 데이터 수집 파이프라인 구축 — 전체 과정 정리

## 배경

기존 구조는 크롤링(별도 저장소, Selenium)으로 네이버 카페에서 견적서 이미지를 긁어온 뒤,
파싱 스크립트와 Mongo 적재 스크립트를 사람이 순서대로 따로 실행해야 했다. 포트폴리오로도
쓸 수 있게 이 전체를 MuneoAI 저장소 안에서 재현 가능하고 테스트 가능한 파이프라인으로
재구축하는 게 목표였다.

## 전체 아키텍처

```
[1단계] 크롤링 (pipeline/crawler.py, scripts/run_crawler.py)
   네이버 카페 게시글 → estimate_data/{지역}/{article_id}/{article_id}.json + 이미지들
           │
           ▼
[2단계] 파싱·저장 (pipeline/ingest.py, scripts/run_ingest.py)
   이미지 → Vision API 파싱(배치/실시간) → 검증·집계 → estimate_cases / review_queue (Mongo)
           │
           ▼
[후속] review_queue 검토 (scripts/promote_review_queue_case.py)
   신뢰도 낮게 라우팅된 사례를 사람이 확인 후 estimate_cases로 승격
           │
           ▼
[평가] 검색 품질 재검증 (eval/refresh_pool.py 등)
   새 사례가 참고 사례 검색(벡터/BM25/리랭킹) 품질에 미친 영향을 precision@k로 측정
```

크롤링과 파싱·저장을 하나로 합치지 않은 이유: 크롤링 결과(`estimate_data/`)를 재사용 가능한
원본으로 남겨둬야 파싱 로직을 고치거나 배치/실시간을 바꿔가며 여러 번 재실행할 수 있고,
배치 API 자체가 "제출 → 몇 시간 뒤 수거" 구조라 크롤링과 동기적으로 합칠 수 없다. 실행법
전체는 `pipeline/results/data_collection_guide.md` 참고.

## 단계별 구축 과정

기존 파싱·검증·집계 로직(Phase 1~5)은 이미 `pipeline/`에 있었다 — `validators.py`,
`parsing.py`, `aggregation.py`, `vision_client.py`/`batch_client.py`, `crawl_filter.py`,
`ingest.py`. 이번에 새로 한 작업은 다음 세 갈래다.

### 1) 크롤러 정식 이관 (Phase 6)

원본 `crawler.py`(577줄)는 "브라우저 페이지 이동(Selenium)"과 "받아온 HTML에서 데이터
추출(BeautifulSoup)"이 함수 하나에 섞여 있어서 브라우저 없이는 파싱 로직 자체도 테스트할
수 없었다. 순수 로직을 먼저 분리하고 그 위에 Selenium 오케스트레이션을 얹는, 이 세션에서
계속 써온 패턴을 그대로 적용했다.

- `pipeline/crawl_parsing.py` — HTML에서 데이터 추출(순수, Selenium 불필요), 20개 테스트
- `pipeline/crawl_region.py` — 지역 감지(순수), 원본 `region_utils.py` 이관
- `pipeline/crawl_download.py` — 이미지 다운로드(순수 네트워크, `requests`만) — 받은
  바이트를 디스크에 쓰기 전에 알려진 보일러플레이트 해시와 대조해 저장 자체를 스킵
- `pipeline/crawler.py` — Selenium 오케스트레이션(로그인·페이지 이동만 담당, 실제 데이터
  추출은 위 세 모듈에 위임). 로그인은 여전히 수동(자동 로그인은 계정 보안/약관 이슈로 배제)
- `scripts/run_crawler.py` — CLI 진입점

자세한 내용: `pipeline/results/crawler_migration.md`

### 2) 대량 실행용 CLI (`scripts/run_ingest.py`)

Phase 5까지는 `process_article()`/`ingest_article()` 함수 자체는 검증됐지만, 여러 건을
반복 실행할 CLI가 없어서 파이썬 인터프리터를 열어 폴더 하나씩 수동으로 넣어야 했다. 이걸
메우면서 두 가지를 같이 했다.

- **배치 모드를 기본으로** — Anthropic Batches API(실시간 대비 50% 저렴). `submit` →
  `status` → `collect` 세 단계로 나누고, 진행 상태(`batch_id`, custom_id → article 매핑)를
  `estimate_data/.batch_state.json`에 저장해 터미널을 꺼도 이어갈 수 있게 했다
  (`collect` 성공 시 자동 삭제)
- **`pipeline/ingest.py` 리팩터링** — `process_article()`에서 "병합→검증→집계" 부분을
  `finalize_record()`로, "라우팅→저장"을 `route_and_save()`로 분리해서 실시간·배치 양쪽이
  같은 로직을 공유하게 했다(배치는 파싱 결과를 다른 경로로 얻을 뿐, 그 이후 처리는 동일)
- 이미 `estimate_cases`/`review_queue`에 저장된 `article_id`는 자동 스킵 — 재실행해도
  API 비용이 중복 발생하지 않게 함(`--force`로 강제 가능)

## 실전 검증 — 664건(신규 92건) 한 사이클 완주

기존 종합프로젝트 저장소의 크롤링 데이터(636건, 253MB)를 `MuneoAI/estimate_data`로 복사해
`.gitignore`에 등록하고, 새 CLI로 실제 한 사이클을 끝까지 돌렸다. 그 과정에서 실사용 중에만
드러나는 버그를 세 개 발견해서 그 자리에서 고쳤다 — 코드만 짜고 끝낸 게 아니라 실제로 돌려보고 나서야 보이는 문제들을 잡았다.

1. **`anthropic-workspace-id` 인증 에러**: identity-linked API 키는 배치 요청 시 어느
   workspace로 실행할지 명시해야 한다는 걸 실제 `submit` 실행에서 처음 발견. `.env`에
   `ANTHROPIC_WORKSPACE_ID`를 두면 `pipeline/vision_client.py`의 `get_client()`가 자동으로
   헤더에 실어 보내도록 수정.
2. **배치 collect가 "이미지 없는 article"을 조용히 유실**: `build_batch_requests()`가
   보일러플레이트만 있거나 이미지가 아예 없는 article은 요청 자체를 안 만들다 보니, 배치
   결과에도 안 나타나서 `estimate_cases`/`review_queue` 어디에도 저장이 안 되는 채로
   사라지고 있었다(92건 신규 중 39건). 실시간 모드(`process_article`)는 이미지 0장이어도
   항상 처리해서 review_queue로 보내는데, 배치 경로만 이 케이스를 놓치고 있었다.
   `cmd_submit`에서 요청이 하나도 없는 article은 즉시 review_queue로 보내고, `cmd_collect`
   에서도 "요청은 했지만 전부 실패한" 경우를 안전망으로 처리하도록 고쳤다.
3. **`eval/build_pool.py` 재실행 시 사람 라벨 전체 삭제**: 코퍼스가 커진 뒤 검색 품질을
   재검증하려고 이 스크립트를 다시 돌리려 했는데, 기존 `pool.json`을 읽지 않고 매번
   덮어써서 이전에 사람이 검토해둔 라벨이 전부 규칙 기반 추정값으로 리셋되는 걸 발견.
   `eval/refresh_pool.py`를 새로 만들어 `(query_id, article_id)` 키로 기존 라벨은
   보존하고 신규 유입분만 검토 대상으로 남기도록 했다.

최종 결과: 664건 전부 Mongo에 저장(estimate_cases 26 + review_queue 638, 이 중 상당수는
이전 백필분). 크롤링 원본 이미지 중 알려진 보일러플레이트 661개(71MB)도 정리.

## 정량 평가 — 검색 품질에 미친 영향

크롤링·파싱이 잘 됐다는 것과 "이 데이터가 실제로 검색 품질에 도움이 되는가"는 별개
질문이다. `eval/refresh_pool.py`(신규) → `flag_review_rows.py` → 사람 검토(142건 중 50건
라벨 변경) → `apply_review_labels.py` → `retrieval_eval.py`로 확인했다.

- **precision@15**(프로덕션 `EstimateEngine.TOP_K`와 일치, 이번에 처음 추가한 지표):
  벡터 단독 32.2% vs 하이브리드+리랭킹 36.9% (**+4.7%p**), recall도 +3.6%p — 개선 확인
- 지금까지 이 프로젝트의 검색 평가가 @5/@10만 봐서 프로덕션이 실제로 쓰는 깊이(top-15)를
  한 번도 직접 측정한 적이 없었다는 것도 이번에 알게 됨
- 자세한 수치·해석·과거 실험 이력: `eval/results/reranker_hybrid_eval.md`

## 추후 개선점

지금 상태는 아키텍처(크롤링/파싱 분리, 순수 로직 테스트, 배치 비용 최적화, confidence
라우팅, 정량 검색 평가)로는 충분히 완결됐다고 본다. 남은 개선 여지는 **운영 성숙도** 쪽이다.

- **전체 사이클 스케줄링**: 지금은 크롤링→파싱→평가까지 사람이 매 단계 명령을 직접 실행함.
  크롤링→파싱(배치 submit까지)은 cron 등으로 자동화할 수 있다 — 단, `collect`는 배치 완료
  시점이 가변적이라 완료 확인 후 실행하는 별도 트리거가 필요
- **모니터링/알림 없음**: 크롤러가 사이트 구조 변경으로 조용히 0건만 수집해도 알아챌 방법이
  없고, 배치 실패도 사람이 `status`를 직접 확인해야 안다
- **보일러플레이트 해시 목록이 수동 큐레이션**: 새 로고/뱃지 이미지가 나오면
  `find_boilerplate_hashes()`로 수동으로 다시 찾아 `KNOWN_BOILERPLATE_HASHES`에 추가해야
  함 — 자동 갱신 트리거 없음
- **배치 파이프라인 통합테스트 부재**: 이번에 발견한 "이미지 없는 article 유실" 버그처럼,
  `submit`→`collect` 전체 흐름을 관통하는 자동 테스트가 없어서 실사용 중 발견에 의존한다.
  가짜 Anthropic Batches 클라이언트로 e2e 테스트를 만들 수 있다
- **검색 품질 평가의 라벨링은 원리상 자동화 불가**: 정답을 사람이 정해야 하는 작업이라 이건
  한계로 남겨도 되지만, `flag_review_rows.py`의 휴리스틱을 더 정교하게 다듬어 검토 대상을
  줄이는 여지는 있음
- **`material_grade`(자재등급) 배치 계산 미착수**: Phase 5부터 이어진 과제 — 전체 코퍼스를
  다시 스캔해야 계산 가능해서 주기적 배치 작업으로 남아있음

## 관련 문서

- `pipeline/results/crawler_migration.md` — 크롤러 이관 상세(Phase 6)
- `pipeline/results/pipeline_e2e_entrypoint.md` — 파싱·검증·집계 연결(Phase 5)
- `eval/results/reranker_hybrid_eval.md` — 검색 품질 평가 전체 이력
