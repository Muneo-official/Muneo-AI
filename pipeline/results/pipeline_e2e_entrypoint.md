# 엔드투엔드 파이프라인 연결 — 결과

## 배경

Phase 1~4로 검증(`validators`)·파싱 후처리(`parsing`)·구조화 출력(`tool_schema`)·API
호출(`vision_client`/`batch_client`)·크롤링 필터(`crawl_filter`)까지 만들었지만, 이걸
"크롤러가 저장한 폴더 하나 넣으면 끝까지 자동으로 도는" 함수로 이어붙인 적은 없었다.
지금까지는 `pipeline/reference/parse_estimates.py`(파싱) + `scripts/migrate_to_mongo.py`
(Mongo 적재) 두 스크립트를 사람이 순서대로 따로 실행해야 했다.

## 구현

- `pipeline/aggregation.py`에 `HAS_KEYWORDS`/`build_check_text`/`build_has_flags` 추가
  — `pipeline/reference/build_rag.py`의 `check_has_keywords()`를 이관. `app/domain/estimate_engine.py`
  가 참고 사례 검색(Stage 1~4 점진적 필터)에 실제로 쓰는 `has_*` 필드라, 파싱만 하고 이 필드를
  안 채우면 저장은 되지만 견적 엔진이 그 사례를 검색으로 못 찾는다
  (`docs/IMPLEMENTATION_LOG.md` 2-12의 Atlas Vector Search Index 필터 누락 사고와 같은 종류의
  실패 모드 — 이번엔 미리 방지)
- `pipeline/ingest.py` 신설
  - `load_article_record(article_dir)`: 크롤러가 저장한 `{article_id}.json` 로드
  - `process_article(article_dir)`: 이미지 전부 파싱(`vision_client.parse_image`, 보일러플레이트
    자동 스킵) → 병합·검증(`parsing.merge_and_validate`) → `cost_*`/`has_*`/`total_cost`/
    `cost_per_pyeong` 집계까지 끝낸 최종 레코드 반환 (DB 저장은 안 함)
  - `ingest_article(article_dir, cases_col, queue_col)`: `process_article()` 결과를
    `routing.route_case()`로 `estimate_cases`/`review_queue`에 저장. 파싱 자체가 실패하면
    (모든 이미지가 견적서가 아니었거나 보일러플레이트만 있었던 경우) confidence 계산 없이
    무조건 `review_queue`로 보낸다

## 실제 검증 — 실제 크롤링 폴더로 엔드투엔드 실행

실제 사례(850833, 이미지 2장)를 `process_article()`로 실행:

```
total_cost: 29,442,800
cost_per_pyeong: 718,117
has_*: 8개 전부 정상 계산 (has_창호/도배/타일/가구/욕실/바닥/전기/조명)
cost_*: 9개 카테고리 정상 계산 (창호/도장/타일/설비/가구/전기/도배/목공/철거)
_validation: {confidence: 1.0, issues: [], reclassification_suggestions: []}
parsed_estimate 항목 수: 58
```

크롤러 JSON 로드 → 이미지 파싱(보일러플레이트 필터 포함) → 병합 → 검증 → `cost_*`/`has_*`
집계까지 한 번의 함수 호출로 끝났고, 결과가 `app/domain/estimate_engine.py`가 실제로
읽는 필드 구조와 일치하는 것을 확인했다 — 이 레코드를 그대로 Mongo에 넣으면 견적 엔진이
바로 참고 사례로 쓸 수 있는 상태다.

## 결론

- 크롤링(별도 저장소) → 파싱(`vision_client`/`batch_client`) → 검증(`validators`) →
  집계(`aggregation`) → 라우팅(`routing`) → 저장까지, `process_article()`/`ingest_article()`
  두 함수로 전체 경로가 실제로 이어지고 동작함을 실측으로 확인
- 이전에는 사람이 파싱 스크립트 → Mongo 적재 스크립트를 순서대로 따로 돌려야 했는데, 이제
  폴더 경로 하나만 넣으면 끝난다

## 한계

- 크롤링 자체(`pipeline/reference/crawler.py`)는 여전히 별도 저장소에 있고 별도로 실행해야
  한다 — `process_article()`은 "크롤러가 이미 저장해둔 폴더"를 입력으로 받는다는 전제.
  **원래 이 에픽을 시작할 때 목표("데이터 수집 파이프라인 전체를 MuneoAI로")엔 크롤러 자체
  이관도 포함돼 있었는데, 5단계로 쪼갤 때 "크롤링 사전 필터링"(필터 추가)만 들어가고
  크롤러 본체 이관은 빠져 있었다** — 이 간극을 뒤늦게 알아채서 **6번 작업(크롤러 정식
  이관)으로 별도 추가**하기로 함. Selenium 기반 네이버 로그인·페이지 스크래핑이라 지금까지
  이관한 것들(순수 API 호출, 단위테스트 가능)과 성격이 다르다는 점을 감안해서 진행할 것
- `material_grade`(자재등급)는 이번 범위에서 채우지 않았다 — 원본(`assign_material_grades()`)
  이 전체 코퍼스의 공종 수 버킷별 평당단가 백분위로 계산하는 방식이라, 새 사례 하나만 놓고는
  계산할 수 없다(전체 코퍼스를 다시 스캔해야 함). 별도의 주기적 배치 작업으로 남겨둔다
- `ingest_article()`의 Mongo 쓰기 로직은 가짜 컬렉션(단위테스트)으로만 검증했다 — 실제
  `estimate_cases`/`review_queue`에 대한 실제 삽입은 이번 세션에서 하지 않았다(운영 반영은
  후속 결정 필요)

## 재현 방법

순수 로직(집계, 검증 연결)은 API 키 없이 확인 가능:
```bash
python -m pytest tests/test_pipeline_aggregation.py tests/test_pipeline_ingest.py -v
```