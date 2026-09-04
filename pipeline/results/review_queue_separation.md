# review_queue 분리 — 결과 및 신뢰도 계산식 수정

## 배경

`pipeline/parsing.py`(파싱 직후 검증 연결)까지 끝낸 뒤, 신뢰도가 낮은 사례를 `estimate_cases`
에서 `review_queue`로 물리적으로 분리하는 작업. `pipeline/routing.py`(임계값 0.7 기준 분기)와
`scripts/separate_review_queue.py`를 만들고 dry-run으로 먼저 확인했다.

## 발견 — 신뢰도 계산식이 "개수" 기반이라 과도하게 많이 걸렸다

1차 dry-run 결과: **572건 중 181건(32%)**이 review_queue 대상으로 나왔다 — `estimate_cases`가
거의 3분의 1이 줄어드는 셈이라 너무 과했다.

원인 분석 결과, 181건 중 **140건(77%)이 순수히 `unknown_category` 경고 개수 때문**이었다
(실제 스키마/평수 오류는 14건뿐). 기존 `validate_known_categories()`는 미인식 category
**종류 수만큼** 경고를 냈고(`_compute_confidence`가 경고당 -0.1), "기타공사"/"단가참고"처럼
애초에 고치지 않기로 정한 항목이 케이스 하나에 4종류만 섞여도 자동으로 confidence 0.6이 되어
review_queue로 빠졌다:

| 미인식 category 종류 수 | confidence | 해당 건수 |
|---:|---:|---:|
| 4 | 0.6 | 56건 |
| 5 | 0.5 | 25건 |
| 6 | 0.4 | 25건 |
| 7 | 0.3 | 15건 |
| 8+ | 0.2 이하 | 19건 |

**문제**: "이름 모를 카테고리가 몇 종류 있냐"는 그 케이스가 참고 사례로서 못 미더운지와 거의
무관하다. 관건은 "그게 견적 전체에서 얼마나 큰 비중이냐"다 — `단가참고` 메모 하나가 전체
금액의 2%면 그 케이스는 여전히 충분히 쓸만하다.

## 수정 — 개수 대신 금액 비중으로 판단

`pipeline/validators.py`의 `validate_known_categories()`를 다시 짰다:
- 미인식 category 항목들의 `amount` 합계 / `total_cost` = **비중**을 계산
- 비중 ≤ 20%: 이슈로 안 봄 (사소한 미분류로 취급)
- 20% < 비중 ≤ 50%: warning
- 비중 > 50%: error (견적의 절반 이상이 뭔지 모르는 카테고리면 실제로 못 미더움)
- 케이스당 이슈를 최대 1개만 냄(예전엔 카테고리 종류마다 하나씩 여러 개)

## 결과

| | 1차(개수 기반) | 수정 후(비중 기반) |
|---|---:|---:|
| review_queue 대상 | 181건 (32%) | **15건 (2.6%)** |

수정 후 15건 전수 확인 — **전부 진짜 문제**였다:
- `size_pyeong_range` error (7건) — 그 article_id 오염 패턴
- `total_consistency` warning + `unknown_category` error 동시 발생 (8건) — 비용의 50% 이상이
  미분류인, 실제로 참고하기 위험한 파싱 결과

가짜 양성(다른 건 멀쩡한데 사소한 미분류 때문에 걸리는 것)이 사실상 사라졌다.

## DB 반영 완료

`scripts/backup_estimate_cases.py`로 재백업(`backups/estimate_cases_20260831T112027Z.json`)
후 `scripts/separate_review_queue.py --apply` 실행. 반영 후 확인:

- `estimate_cases`: 572 → **557건**
- `review_queue`: 0 → **15건** (신규 컬렉션)

## 재현 방법

```bash
python -m pytest tests/test_pipeline_validators.py tests/test_pipeline_routing.py -v
python -m scripts.separate_review_queue            # dry-run, 15건 확인
python -m scripts.backup_estimate_cases            # --apply 전 필수
python -m scripts.separate_review_queue --apply    # 실제 이관
```
