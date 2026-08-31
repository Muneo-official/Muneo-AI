# Vision API 호출 코드 정식 이관 — 결과

실행일: 2026-09-01

## 배경

Phase 1(프롬프트)·Phase 2(tool use)까지 파싱 품질을 개선했지만, 실제 Anthropic API를
호출하는 코드는 여전히 `pipeline/reference/`(참고용, gitignore)에만 있었다 — 다음
크롤링 배치를 실제로 돌리려면 이 코드가 정식으로 `pipeline/`에 있어야 한다. 이번
단계는 이미지 전처리부터 실시간/배치 API 호출까지 전부 이관하는 작업이다.

## 구현

- `pipeline/image_prep.py` — 이미지 리사이즈·세로 청크 분할 (순수 함수, PIL만 의존)
- `pipeline/parsing.py`에 `merge_chunk_results()` 추가 — 같은 이미지의 여러 청크를
  합치는 함수(기존 `merge_parsed_results()`는 서로 다른 이미지/페이지를 합치는 함수라
  역할이 다름 — 총금액이 다르면 "다른 견적서가 섞였다"고 판단해버려서 청크에는 못 씀)
- `pipeline/vision_client.py` — 실시간 API 호출. `pipeline/tool_schema.py`(Phase 2,
  category enum 강제)를 그대로 사용. `parse_image(path)` 하나로 전처리→청크별 호출→병합까지
- `pipeline/batch_client.py` — Anthropic Batches API(공식 약 50% 할인)로 여러 이미지를
  한 번에 처리. 원본 크롤러의 특정 폴더 구조에 종속되지 않도록 `(article_id, image_paths)`
  목록만 받게 일반화. 상태 저장(어느 배치를 제출했는지)은 호출자 책임으로 남김
- `requirements.txt`에 `anthropic==1.2.0`, `Pillow==12.3.0` 추가 (이제 진짜 런타임 의존성)

## 실제 검증 — 엔드투엔드 실호출

`pipeline/vision_client.parse_image()`를 실제 크롤링 이미지(850258_1.png, 청크 2개)로
호출:

```
is_estimate: True
total_cost: 48,605,600
항목 수: 70
enum 밖 카테고리: []
```

전처리(청크 분할)→API 호출(tool use)→청크 병합까지 한 번의 함수 호출로 정상 동작 확인.

## 발견한 버그 — Phase 2와 Phase 3(검증)가 서로 안 맞물렸다

`merge_and_validate()`로 이어서 검증까지 실행했더니, 정상 데이터인데도 `unknown_category`
경고가 떴다:

```
'미인식 category가 total_cost의 48%를 차지함 (가구, 공과잡비, 도장, 설비, 창호)'
```

"가구", "공과잡비", "도장", "설비", "창호"는 전부 정상적인 14개 정규화 카테고리다.
원인: `normalize_category()`는 `CATEGORY_NORM`의 **키**(원본 표기, 예: "가구공사")에서만
조회하는데, tool use(Phase 2)는 **이미 정규화된 값**("가구")을 직접 출력한다. 그래서
`normalize_category("가구")`를 호출하면 "가구"라는 키가 없어 `None`을 반환하고
"미인식"으로 잘못 처리됐다.

**의미**: Phase 2에서 tool use를 설계할 때, Phase 3에서 그 출력을 검증 레이어에 실제로
연결해보고 나서야 드러난 통합 버그다 — 각 단계를 따로 테스트할 때는 안 보이고, 실제로
이어붙였을 때만 나타나는 전형적인 통합 오류. 매 단계 실제 데이터로 엔드투엔드 검증을
계속해온 이유가 바로 이거다.

**수정**: `normalize_category()`에 "입력이 이미 `NORMALIZED_CATEGORIES`에 속하면 그대로
반환" 분기를 최상단에 추가. pytest로 회귀 테스트 추가(14개 정규화 카테고리 전부에 대해
자기 자신으로 정규화되는지 확인).

**수정 후 재검증**: 같은 데이터로 `unknown_category` 경고가 사라지고 confidence가
올바르게 계산되는 것을 확인.

## 결론

- 이미지 전처리→API 호출(tool use)→청크 병합→검증까지 전체 경로가 실제로 연결되고
  동작함을 실측으로 확인
- 파이프라인 단계를 나눠서 만들 때 흔히 생기는 함정(각 단계는 정상인데 이어붙이면
  깨지는 경우)을 실제로 겪고 고침 — 이후 단계(엔드투엔드 연결, 5번)를 진행하기 전에
  발견해서 다행

## 한계

- 배치 모드(`pipeline/batch_client.py`)는 요청 생성(`build_batch_requests`)만 실제
  이미지로 테스트했고, 제출→상태확인→결과수집까지 실제 Batches API로는 검증하지
  않았다 — 배치는 완료까지 최대 24시간 걸릴 수 있어 이번 세션에서는 실시간 경로만
  실측했다
- 상태 저장(어느 배치를 제출했는지)을 호출자 책임으로 남겨서, 실제 운영에 쓰려면
  `scripts/`에 상태 파일 관리 스크립트가 별도로 필요하다

## 재현 방법

순수 로직은 API 키 없이 확인 가능:
```bash
python -m pytest tests/test_pipeline_image_prep.py tests/test_pipeline_parsing.py tests/test_pipeline_batch_client.py -v
```

실제 API 호출 테스트는 `ANTHROPIC_API_KEY`와 로컬 크롤링 이미지가 필요하며 비용이
발생하는 작업이라 이 저장소에는 재현 스크립트를 커밋하지 않았다(스크래치 전용으로 실행).
