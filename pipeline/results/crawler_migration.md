# 크롤러 정식 이관 (Phase 6) — 결과

## 배경

Phase 1~5로 파싱·검증·집계·라우팅·엔드투엔드 연결(`process_article`/`ingest_article`)
까지 MuneoAI `pipeline/`로 옮겼지만, 이 에픽을 시작할 때 목표("데이터 수집 파이프라인
전체를 MuneoAI로")엔 크롤러 자체(`pipeline/reference/crawler.py`, Selenium 기반 네이버
카페 스크래핑) 이관도 포함돼 있었다. 5단계로 쪼갤 때 "크롤링 사전 필터링"(해시 기반
보일러플레이트 차단, Phase 4)만 들어가고 크롤러 본체 이관은 빠져 있었던 걸 뒤늦게
발견해 6번 작업으로 별도 추가했다(`pipeline_e2e_entrypoint.md` 한계 절 참고).

## 구현

원본 `crawler.py`(577줄)는 "브라우저 페이지 이동(Selenium)"과 "받아온 HTML에서 데이터
추출(BeautifulSoup)"이 함수 하나에 섞여 있어서, 브라우저 없이는 파싱 로직 자체도
테스트할 수 없었다. 이번 세션에서 이미 여러 번 쓴 패턴(순수 로직 먼저 분리 → 단위테스트
→ 그 위에 I/O 계층 얹기)을 그대로 적용해 3개 모듈로 쪼갰다.

- **`pipeline/crawl_parsing.py`** (순수, Selenium 불필요) — HTML 문자열을 받아 데이터를
  뽑아내는 로직만 모음: `is_valid_estimate_image`/`upgrade_image_url`(URL 필터·화질
  업그레이드), `parse_article_rows`(게시글 목록), `extract_article_id`,
  `collect_image_urls_from_html`(이미지 URL 수집·중복제거 — 원본에서 `_parse_detail`과
  `collect_linked_images`에 중복돼 있던 로직을 하나로 통합), `parse_estimate_link`,
  `parse_estimate_detail`(공사정보+이미지 추출, 본문 비면 `None`), `parse_request_body`,
  `extract_pcarpenter_links`. 고정 HTML 픽스처로 12개 테스트 전부 실측 통과.
- **`pipeline/crawl_region.py`** (순수) — `pipeline/reference/region_utils.py`의
  `detect_region()`을 그대로 이관. location 필드 우선, 없으면 본문에서 지역명 탐색,
  업체명 패턴("서울24호") 오인식 방지. 5개 테스트 통과.
- **`pipeline/crawl_download.py`** (순수 네트워크, `requests`만 사용) — `download_images()`.
  개별 이미지 다운로드 실패는 건너뛰고 계속 진행(이미지 하나가 만료/삭제됐다고 나머지까지
  못 받으면 안 됨). 받은 바이트를 디스크에 쓰기 전에 `crawl_filter.KNOWN_BOILERPLATE_HASHES`
  와 대조해 알려진 보일러플레이트는 아예 저장하지 않는다(아래 "실사용 중 발견한 문제"
  참고). 4개 테스트(정상 저장, 실패 스킵, 보일러플레이트 해시 스킵, 폴더 자동 생성) 통과.
- **`pipeline/crawler.py`** (Selenium 오케스트레이션, 자동 테스트 대상 아님) — 로그인,
  iframe 진입, 페이지 이동만 담당하고 실제 데이터 추출은 위 세 모듈에 위임:
  `get_driver`, `naver_login`(여전히 수동 — `input()`으로 사람이 로그인 완료 후 진행,
  네이버 계정 보안/약관 이슈로 자동 로그인은 하지 않음), `enter_iframe`,
  `get_user_articles`, `get_estimate_link_from_post`, `get_estimate_detail`,
  `get_request_body`, `collect_linked_images`, `crawl_user`(메인 오케스트레이터).

## URL 필터(NOISE_KEYWORDS) vs 해시 필터(crawl_filter.py) — 역할 분담 확인

둘 다 "견적서 아닌 이미지 거르기"가 목적이지만 서로 다른 단계에서 동작해 중복이 아니라
상호보완이다.

- `NOISE_KEYWORDS`(크롤링 단계, URL 문자열 패턴): 로고/프로필뱃지처럼 **URL 자체에
  식별 가능한 패턴**이 있는 이미지를 다운로드하기 전에 거른다. 비용 0(요청조차 안 함).
- `crawl_filter.KNOWN_BOILERPLATE_HASHES`(파싱 단계, 파일 내용 해시): URL 패턴에 안
  걸리지만(파일명이 매번 다르게 발급됨) **바이트 단위로 완전히 동일한 파일**이 여러
  게시글에 반복 첨부되는 경우를 잡는다. 실측(1,626개 이미지)에서 40.3%가 이 케이스였다
  — URL 필터만으로는 못 잡는 게 대다수라는 뜻.

즉 URL 필터는 "다운로드할 가치도 없는 것"을 먼저 걸러 크롤링 비용을 줄이고, 해시 필터는
"다운로드는 됐지만 실제로는 보일러플레이트인 것"을 파싱 직전에 한 번 더 거른다. 하나로
합칠 수 없다 — 전자는 다운로드 전에만 쓸 수 있는 정보(URL)를, 후자는 다운로드 후에만
얻을 수 있는 정보(파일 내용)를 쓴다.

## 실사용 중 발견한 문제 — 다운로드 단계 보일러플레이트 미필터

기존 크롤링 데이터(`estimate_data/`, 종합프로젝트에서 복사)를 실제로 열어보다가 사례
901596에서 두 번째 이미지가 견적서가 아니라 "박목수" 앱 로고인 걸 발견했다. 원인 확인:

- URL(`.../25B125D725B825B22.png`)엔 `NOISE_KEYWORDS`(`logo_icon`, `로고` 등) 중 어떤
  패턴도 없었다 — 네이버 카페가 파일명을 매번 랜덤 인코딩해서 URL만 보고는 로고인지
  구분할 방법이 없다. URL 필터의 근본적 한계.
- 파일 내용의 SHA-256 해시를 계산해보니 `KNOWN_BOILERPLATE_HASHES`의 127회 반복 해시와
  정확히 일치했다 — 즉 파싱 단계(`vision_client.parse_image`)에서는 이미 정상적으로
  걸러지고 있었다(API 비용 0, 최종 결과에도 포함 안 됨). 실질적인 오류는 아니었다.
- 다만 크롤링 단계(`download_images`)엔 해시 체크가 없어서 **디스크에는 저장**되고
  있었다 — 파싱 시점에만 버려지니 디스크 용량과 다운로드 트래픽이 낭비되는 구조였다.

`download_images()`에 `KNOWN_BOILERPLATE_HASHES` 대조를 추가해 받은 바이트를 파일로
쓰기 전에 걸러내도록 고쳤다. 저장된 이미지에만 순번을 매기므로(`{folder}_{저장된 개수}`),
걸러진 이미지는 인덱스를 차지하지 않는다. 어차피 파싱 단계에서도 같은 해시 목록으로
한 번 더 걸러지므로 이중 방어이며, 둘 중 하나를 없애도 정확성엔 문제없지만 다운로드
단계에서 먼저 거르는 게 자원 낭비를 막는다.

## 출력 형식 호환성 확인

`pipeline/ingest.py`의 `load_article_record`/`process_article`이 기대하는 구조
(`{article_dir}/{article_id}.json` — `article_id`/`region`/`size_pyeong`/
`request_body_text`/`local_images` 포함)를 `crawl_user()`가 그대로 만족하는지 코드
대조로 확인했다. `local_images`는 `download_images()`가 반환한 실제 저장 경로 리스트를
문자열로 담고, `request_body_text`는 링크가 없으면 빈 문자열로 채워 `process_article()`의
`build_has_flags()` 호출에서 `None` 관련 예외가 나지 않게 했다.

## 검증

- `python -m pytest tests/test_crawl_parsing.py tests/test_crawl_download.py tests/test_crawl_region.py -v`
  → 21개 전부 통과
- `python -m pytest tests/ -q` (전체 스위트) → 115개 전부 통과, 기존 기능 회귀 없음
- `ruff check pipeline/crawler.py pipeline/crawl_parsing.py pipeline/crawl_download.py pipeline/crawl_region.py`
  → 통과 (미사용 import 1건 발견 후 제거)
- `import pipeline.crawler` — Selenium 드라이버를 실제로 띄우지 않고도 모듈 임포트 자체는
  에러 없이 됨을 확인 (지연 실행 구조가 깨지지 않았는지 확인 차)

## 한계

- **실제 크롤링 실행 자체는 이번 세션에서 하지 않았다.** `naver_login()`이 실제 네이버
  로그인(사람의 계정, 수동 입력)을 요구해서 자동으로 검증할 수 없다 — 코드 구조·출력
  형식 호환성만 정적으로 확인했다. 실사용 전 사용자가 직접 한 번 실행해 로그인부터
  이미지 저장까지 실제로 돌아가는지 확인했다.
- `crawl_user()`의 Selenium 오케스트레이션 함수들(`get_driver`~`collect_linked_images`)은
  브라우저가 필요해 단위테스트 대상이 아니다 — 원본과 동일한 구조적 한계다. 신뢰도는
  "각 함수가 호출하는 순수 파싱 로직이 정확한가"로 대신 확보했다(위 20개 테스트).
- 원본의 `estimates.json`(전체 결과 취합 파일) 저장은 이관하지 않았다 — `ingest.py`가
  개별 `{article_id}.json`만 읽으므로 불필요한 산출물이었다. 필요해지면 `crawl_user()`
  반환값(`results` 리스트)을 호출부에서 직접 dump하면 된다.
- `MAX_PAGES`/`SLEEP_SEC`/`CAFE_ID` 등 설정값은 원본 그대로 모듈 상단 상수로 유지했다
  — 별도 설정 파일화는 이번 범위 밖.
