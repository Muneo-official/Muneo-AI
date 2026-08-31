# 파싱 파이프라인에 검증 연결 — 결과

실행일: 2026-08-31

## 배경

`pipeline/validators.py`(검증 레이어)를 기존 `estimate_cases`에 소급 적용해서 문제를 찾고
고쳤지만(`pipeline/results/validation_report.md`), 이건 **이미 쌓인 데이터에 대한 사후 처리**일
뿐이었다. 파싱 로직(`pipeline/reference/parse_estimates.py`) 자체는 그대로라 다음 크롤링
배치에서도 같은 문제(카테고리 불일치 등)가 재발할 수 있었다. 이 작업은 "파싱 결과가 나오자마자
검증까지 자동으로 붙는" 구조를 만든다.

## 범위 결정 — 전체 이관이 아니라 순수 로직만

`pipeline/reference/parse_estimates.py`(974줄)는 크게 두 부분으로 나뉜다:

1. **인프라**: 이미지 다운로드·리사이즈·청크 분할, Anthropic Vision API 호출(실시간/배치 모드)
   — 파일 I/O·네트워크·과금이 딸려서 이 환경에서 재현·테스트가 안 됨
2. **순수 로직**: API가 반환한 JSON을 후처리·병합하는 부분(`_fix_column_swap`,
   `_remove_aggregate_items`, `merge_parsed_results` 등) — 입력이 dict, 출력도 dict라
   외부 의존성 없이 완전히 테스트 가능

이번 작업에서 실제로 필요한 건 "검증을 언제 연결하느냐"고, 그 지점은 2번(병합 직후)이다.
1번(이미지·API 인프라)을 통째로 옮기는 건 테스트도 못 하면서 인프라 코드만 복제하는 셈이라
이번 범위에서 제외했다 — `pipeline/reference/`에 참고용으로만 남겨둔다.

## 구현

- `pipeline/parsing.py` — `merge_parsed_results()`(순수 로직 이관) + 신규 `merge_and_validate()`
- `merge_and_validate(results, size_pyeong)`: `merge_parsed_results()` 실행 후 바로
  `pipeline.validators.validate_case()`를 호출해 결과에 `_validation`(confidence, issues,
  reclassification_suggestions) 필드를 붙인다 — 기존 `_parse_warning`이 붙는 것과 같은 위치·
  성격이지만, 범위가 카테고리 인식·평수 범위까지 넓어졌다.

## 검증

`tests/test_pipeline_parsing.py` 7개 — 특히:
- 실제 발견 사례(article_id=890396, "도어공사"로 분류된 가구 문짝)와 동일한 패턴을 넣으면
  `merge_and_validate()`가 **파싱 직후 바로** 재분류 제안을 냄
- `size_pyeong`에 article_id 숫자가 잘못 들어간 패턴(`docs/IMPLEMENTATION_LOG.md` 2-4)도
  파싱 직후 바로 `size_pyeong_range` error로 잡힘

전체 pytest 45개 통과(기존 38개 + 신규 7개).

## 한계 — 아직 실제 크롤링 배치에는 연결 안 됨

- `merge_and_validate()`는 만들었지만, **실제 Vision API를 호출하는 코드(`pipeline/reference/parse_estimates.py`
  의 `process_article()`/`cmd_batch_apply()`)가 이 함수를 쓰도록 바뀐 건 아니다.** 그 인프라
  코드는 여전히 원래 `merge_parsed_results()`를 직접 호출한다.
- 다음에 실제로 크롤링을 돌릴 때는, 그 인프라 코드의 `merge_parsed_results(...)` 호출을
  `pipeline.parsing.merge_and_validate(..., size_pyeong=...)`로 바꿔주는 작업이 별도로 필요하다
  (이 저장소가 아니라 실제 크롤러가 도는 환경에서).
- `_validation` 필드가 저장된 JSON에 붙은 뒤, 그걸 Mongo 이관 스크립트가 같이 옮기도록 하는
  것도 아직 안 함 — `estimate_cases`에 `parsing_confidence`를 원본부터 갖고 있게 하려면
  이관 스크립트도 손봐야 한다.

## 재현 방법

```bash
python -m pytest tests/test_pipeline_parsing.py -v
```

API 키나 실제 이미지 없이, 순수 dict 입력만으로 전부 검증 가능하다.
