# 리스크 진단 API 이관 + 가격 이상 탐지 추가 — 결과

## 배경

리스크 진단 API(견적서 이미지 업로드 → 누락/중복/불분명 항목 탐지)가 원본 졸업 프로젝트
(`종합프로젝트/risk_detector`, `routes/risk_detector.py`)에만 있고 MuneoAI로 이관된 적이
없었다. PM이 "이미 구현된 API 아니냐"고 물었을 때 dev/main 어디에도 없다는 걸 확인하며
발견했다. 기존 로직은 전부 키워드 매칭 룰이라 "가격이 시세 대비 적정한가"는 아예 안 본다
— 이번 작업은 기존 룰(누락/중복/불분명/고층 양중비)은 그대로 유지한 채, MuneoAI가 이미
갖고 있는 `estimate_cases` 코퍼스 + `EstimateEngine`을 재사용해 카테고리별 가격 이상
탐지를 추가하는 것. 공종 누락 탐지(공종이 통째로 빠졌는지)는 이미지만으로는 정상적인
부분 시공과 구분이 안 돼(`fix/partial-scope-cost-underestimation` PR에서 이미 다룬
문제와 동일한 이유) 이번 스코프에서 제외했다.

## 카테고리 3계통 문제

이 코드베이스엔 서로 다른 카테고리 어휘가 3개 있었다:

1. `pipeline/categories.py`의 `NORMALIZED_CATEGORIES`(14개) — `estimate_cases`의
   `cost_<카테고리>` 필드명과 1:1 대응. `pipeline/tool_schema.py`가 Vision 파싱 시 이
   enum을 강제한다.
2. `app/schemas/estimate.py`의 `공종_리터럴`(12개, 견적 생성 시 사용자가 고르는 값) —
   `estimate_engine.py`의 `공종_TO_KEYS`가 이걸 다시 `cost_*` DB 필드로 매핑한다.
3. `risk_detector`의 자체 `PROCESS_CATEGORY_MAP`/`PROCESS_KEYWORDS`(11개, analyzer 전용).

risk_detector 파서를 자체 정규식 JSON 추출기(`risk_detector/parser.py`)에서
`pipeline/vision_client.call_vision_api`(tool-use)로 교체하면서, 파싱된 line_item의
`category`가 자동으로 1번(14개 DB taxonomy)을 따르게 됐다. 그래서 가격 비교는 2번
어휘(`EstimateEngine.extract_costs`)를 거치지 않고, `case.get(f"cost_{category}")`로
직접 읽는다(`app/domain/risk_price_checker.py`). analyzer.py의 3번 어휘는 기존 룰
유지 목적이라 손대지 않았다 — 두 계통은 이번 작업에서 서로 다른 목적으로 병행 사용된다.

## 구현

- **순수 로직 이관** (`app/schemas/risk.py`, `app/domain/risk_constants.py`,
  `risk_models.py`, `risk_analyzer.py`, `risk_formatter.py`) — 원본 로직 100% 동일,
  import 경로만 조정. 유닛테스트 11개 신규(`tests/test_risk_analyzer.py`,
  `test_risk_formatter.py`).
- **이미지 전처리 통합** — `risk_detector/chunker.py`와 `pipeline/image_prep.py`의
  리사이즈+세로분할 로직이 완전히 동일해서 중복 구현하지 않고, bytes 입력을 받는
  `prepare_chunks_from_bytes()`만 `image_prep.py`에 추가했다. `chunker.py`는
  이관하지 않음.
- **파서 교체** — `risk_detector/parser.py`(정규식 JSON 추출)는 이관하지 않고
  `pipeline/vision_client.call_vision_api()`(tool-use, category enum 강제)로 완전히
  대체. 여러 청크 결과 병합은 `pipeline/parsing.merge_chunk_results()`를 그대로 쓰고,
  여러 이미지(같은 견적서의 여러 페이지) 간 병합·중복제거는 원본 `service.py`의 자체
  로직을 그대로 이관(`_merge_across_images`).
- **가격 이상 탐지** (`app/domain/risk_price_checker.py`, 신규) — 새 가격 통계 로직을
  만들지 않고 기존 `EstimateEngine.build_query()`/`retrieve_cases()`(평수+지역 매칭
  유사 사례 검색)를 그대로 재사용. line_item 합산 금액이 유사 사례 가격 범위를 벗어나면
  기존 "불분명" 이슈 타입에 합류시킨다(새 이슈 타입을 만들지 않음 — formatter가 그대로
  처리 가능). 비교 사례가 3건 미만이면 근거 부족으로 체크를 스킵하고 로그만 남긴다.
  - **가격 범위는 `EstimateEngine.cost_range()`(P25~P75)를 그대로 안 쓰고 이 모듈
    전용으로 `_price_range()`(P10~P90)를 따로 뒀다.** 처음엔 재사용했었는데, 실제
    크롤링 견적서 4건으로 end-to-end 테스트해보니 카테고리 하나당 P25~P75(중간 50%)
    밖으로 벗어날 확률이 이미 ~50%였다 — 이 서비스가 카테고리를 8~12개씩 독립적으로
    체크하니 "적어도 하나는 벗어날 확률"이 1-0.5^8 ≈ 99.6%로 치솟아, 사실상 모든
    견적서가 가격 이상 판정을 몇 건씩 달고 나왔다(버그가 아니라 통계적으로 당연한
    결과였음 — 아래 "실제 이미지 검증" 참고). `cost_range()` 자체를 넓히면
    `generate()`의 실제 가견적 산출(정확도가 생명)에 영향을 주므로, 체크 전용 함수로
    분리해 P10~P90으로 넓혔다.
  - 자유 텍스트 지역(`region: str`)을 엔진이 기대하는 서울/수도권/지방 버킷으로
    바꾸기 위해 새 지역 사전을 만들지 않고 `pipeline/crawl_region.REGION_PATTERNS`
    (raw 지역명 탐지)와 `estimate_engine.REGION_MAP`(3버킷 역매핑)을 조합만 했다.
  - risk_detector엔 사용자가 고른 공종 어휘(`공종_리터럴`)가 없어서, `retrieve_cases`
    호출 시 `공종=[]`로 넘겨 has_* 필터는 건너뛰고 평수+지역 필터만 적용한다.
  - 유닛테스트 8개 신규(`tests/test_risk_price_checker.py`, `EstimateEngine`을
    mock으로 넘겨 범위 안/밖/표본부족/P10~P90 완화 효과 케이스 검증).
- **서비스/저장소/라우터 배선** — `RiskDetectorService`(`app/domain/risk_detector_service.py`)
  생성자에 `EstimateEngine`을 주입받아 가격 체크를 호출한다. Vision API 호출은
  `run_in_threadpool`로 감싸 이벤트 루프를 막지 않게 했다(원본은 서비스 전체가 동기
  함수였고, async 라우트 안에서 직접 호출되고 있어 블로킹 위험이 있었다 — 이관하면서
  같이 고쳤다). `RiskReportRepository`(`app/repositories/`)는 `EstimateRepository`와
  동일한 패턴. `risk_reports` 컬렉션은 별도 DB를 새로 만들지 않고 기존 `estimate_db`
  안에 뒀다 — Mongo 클라이언트를 하나만 `lifespan`에서 관리하는 구조라 DB를 쪼갤
  운영상 이유가 없었다. 라우터(`app/api/routers/risk_detector.py`)는
  `app/api/routers/estimates.py`와 같은 DI/헤더/레이트리밋 패턴을 따른다 — `/analyze`는
  이미지 여러 장 × 청크마다 Vision API를 호출하는 무거운 엔드포인트라
  `/estimates/generate`(10/min)보다 세게 5/min(사용자별) + 50/min(전역)으로 제한했다.
  라우터 배선 테스트 9개, 서비스 배선 테스트 5개 신규.

## 실제 이미지 검증 (Vision API + Mongo Atlas 실호출)

Mock이 아니라 실제 크롤링된 견적서 이미지(`estimate_data/`)로 `/risk-detector/analyze`
전체 파이프라인을 여러 차례 돌려봤다. 그 과정에서 발견해 고친 것들:

1. **formatter가 "정상" 항목을 목공 말고는 거의 못 띄우던 버그** — `PROCESS_CATEGORY_MAP`이
   원본 자체 파서의 "-공사" 접미사 표기만 알고 있었는데, 지금 파서(`pipeline/parsing.py`)는
   접미사 없는 표기("철거", "도배" 등)를 낸다. `formatter._process_from_category()`는
   exact match만 하고 fallback이 없어서, 정상 line_item이 리포트에서 통째로 빠지고
   있었다(analyzer의 누락/불분명 체크는 키워드 fallback 덕에 영향 없었음). 각 카테고리
   리스트에 접미사 없는 표기를 추가해서 해결(`tests/test_risk_formatter.py`에 회귀
   테스트 추가).
2. **가격 이상 탐지가 카테고리 대부분을 이상으로 잡던 문제** — 위 "가격 이상 탐지"
   절 참고. 실측: 4개 견적서(서울/경기/부산/대구, 24~74평) 모두 8~12개 카테고리 중
   5~11개가 이상으로 잡혔다. 원인은 P25~P75(중간 50%)가 카테고리 하나당 이미 ~50%
   확률로 벗어나는 범위였기 때문 — `_price_range()`를 P10~P90으로 넓혀 재검증하니
   같은 견적서(article_id=849623)에서 이상 카테고리 수가 9개 → 7개로 줄었고, 남은
   7개는 실제로 시세 대비 뚜렷이 벗어난 값들이었다(예: 가구 10,891,000원 vs 넓힌
   범위 1,605,000~8,430,000원 — 여전히 범위 밖).
3. **`pipeline/parsing.py`의 `total_cost` 파싱 크래시** — Vision API가 가끔
   `total_cost`를 정수 대신 문자열 `"<UNKNOWN>"`으로 반환하는 경우가 있는데,
   `merge_chunk_results()`/`merge_parsed_results()`가 무조건 `int()`로 캐스팅해서
   그대로 죽었다. 이건 risk_detector가 아니라 **크롤러/적재 파이프라인이 이미 쓰던
   기존 공용 코드**의 버그라, 실사용에서 크롤러 쪽도 같은 이미지를 만나면 똑같이
   터졌을 것 — `_safe_int()` 헬퍼를 추가해 캐스팅 실패 시 0으로 폴백하도록 6곳 전부
   고쳤다(`tests/test_pipeline_parsing.py`에 회귀 테스트 3개 추가).
4. **서버 프로세스에서 `ANTHROPIC_API_KEY` 인증 실패** — `app/core/config.py`
   (pydantic-settings)는 `.env`를 읽어 자기 `Settings` 객체에만 채우지 `os.environ`엔
   안 넣는다. `pipeline/vision_client.py`의 `anthropic.Anthropic()`은 `os.environ`에서
   직접 키를 찾는데, 지금까지 이 값이 필요했던 건 `load_dotenv()`를 스스로 부르는
   독립 스크립트들뿐이었다 — 리스크 진단이 **서버 프로세스에서 Vision API를 처음
   실시간 호출하는 경로**라 이 gap이 처음 드러났다. `app/main.py`에 `load_dotenv()`
   한 줄 추가로 해결.

수정 후 Postman으로 다시 호출해 실제 리포트(누락 1건, 불분명 10건 — 모호표현 3건 +
가격이상 7건)가 정상적으로 조립되는 것까지 확인했다.

## 검증

- `python -m pytest tests/test_risk_*.py -q` → 32개 전부 통과
- `python -m pytest -q --ignore=pipeline/reference` (전체 스위트) → 154개 전부 통과,
  기존 기능 회귀 없음
- `ruff check` (신규/수정 파일 전체) → 통과
- 실제 견적서 이미지 5장(단건 1 + 배치 4)으로 `/risk-detector/analyze` 엔드투엔드 실행,
  Postman으로 서버 프로세스 경유 호출까지 확인(위 "실제 이미지 검증" 참고)

## 한계

- **욕실 vs 설비 카테고리 분류가 이미지마다 다를 수 있음** — 같은 종류의 욕실 설비
  품목(양변기/세면기 등)이 어떤 이미지에서는 category="욕실"로, 다른 이미지에서는
  category="설비"로 파싱됐다. Vision 모델 자체의 카테고리 경계 판단 차이라 risk_detector
  코드로 고칠 부분은 아니고, 코퍼스 전체에도 이미 존재하는 애매함으로 보인다.
- **analyzer.py의 11개 process 어휘가 pipeline의 14개 카테고리를 전부 못 덮는다** —
  `창호`/`필름`/`공과잡비`/`확장` 카테고리 항목은 `PROCESS_CATEGORY_MAP`/`PROCESS_KEYWORDS`
  어디에도 안 걸려 `RiskAnalyzer`의 누락/중복/불분명 체크 대상에서 아예 빠진다(원본에도
  있던 한계 — 이번에 새로 만든 문제는 아님, `test_risk_analyzer.py`의
  `test_unrecognized_category_and_description_dropped`로 이 동작을 명시적으로 고정해둠).
  가격 이상 탐지는 이 4개 카테고리도 정상적으로 커버한다(`_CATEGORY_TO_PROCESS`가 raw
  라벨을 그대로 process로 써서 formatter가 처리).
- **공종 누락 탐지는 스코프 밖** — 시공 범위(전체/부분, 어떤 공종을 계약했는지) 입력이
  요청 스펙에 없어 이미지만으로는 정상적인 부분 시공과 구분이 안 된다. 필요해지면
  `AnalyzeRiskCommand`에 시공범위 필드를 추가하는 후속 작업으로 분리.
- **Vision API 호출은 순차 실행** — 원본은 `ThreadPoolExecutor`(설정 가능한 워커 수)로
  청크를 병렬 파싱했는데, 이번엔 `run_in_threadpool`로 이벤트 루프만 안 막게 하고
  청크별로 순차 `await`한다. 이미지/청크가 많으면 원본보다 느릴 수 있음 — 필요해지면
  `asyncio.gather`로 병렬화하는 걸 후속으로 고려.
