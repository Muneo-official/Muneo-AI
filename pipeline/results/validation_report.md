# 데이터 파이프라인 검증 레이어 — 1차 결과

실행일: 2026-08-31
실행 스크립트: `scripts/validate_existing_cases.py`

## 배경

크롤링 → 견적서 이미지 → LLM 비전(Claude) 구조화 추출 → Mongo 저장으로
이어지는 기존 파이프라인에 **추출 결과를 검증하는 단계가 없었다**. 오늘 조사(리랭커 길이 편향,
창호/필름 가격 편차)에서 실제 데이터 품질 문제(카테고리 오분류, size_pyeong 오염)가 이 검증
부재에서 비롯됐다는 게 드러나, `pipeline/`에 독립 검증 모듈을 새로 만들었다.

## 구성

- `pipeline/schemas.py` — 파싱 결과(`LineItem`, `ParsedEstimate`)의 구조 스키마 (Pydantic)
- `pipeline/categories.py` — category 원본 표기 → 정규화 매핑(`CATEGORY_NORM`, 참고 저장소의
  `build_rag.py`에서 이관) + 쉼표 결합 복합 표기를 분해하는 `normalize_category()`
- `pipeline/validators.py` — 논리 검증 규칙 4종 + 재분류 제안 + 신뢰도 스코어링
- `scripts/validate_existing_cases.py` — `estimate_cases` 572건에 소급 적용하는 진단 스크립트
  (DB는 건드리지 않음, 순수 리포트)

## 검증 규칙

| 규칙 | 내용 | 근거 |
|---|---|---|
| `size_pyeong_range` | 5~200평 밖이면 반려 | 크롤러의 `re.search(r"(\d+)\s*평", body_text)`가 본문 전체 첫 매치를 그대로 신뢰 — article_id 숫자가 잘못 잡힌 사례 실측(`docs/IMPLEMENTATION_LOG.md` 2-4) |
| `total_consistency` | `line_items` 합계 vs `total_cost` 오차 20% 초과 시 경고 | 파싱 단계(`parse_estimates.py`)의 `_parse_warning`과 동일 임계값 — 파싱 시점 경고와 사후 재검증이 어긋나지 않게 |
| `unknown_category` | `CATEGORY_NORM`(+쉼표 분해)로도 인식 못 하는 category 경고 | `build_category_costs()`가 미인식 category를 조용히 버리는 걸 실측(아래 "발견" 참고) |
| `door_reclassification` (제안, 반려 아님) | `"도어공사"` 항목 중 가구 키워드(붙박이장·신발장 등) 포함 시 재분류 제안 | `eval/results/pricing_gap_diagnostic.md`에서 실제 사례(890396)로 확인된 패턴 |

## 발견 — CATEGORY_NORM이 파싱 프롬프트 자체와 어긋나 있었다

`scripts/validate_existing_cases.py`를 처음 돌려서 나온 미인식 category 상위 목록(수정 전):

| category | 건수 | 합계 | 원인 |
|---|---:|---:|---|
| `수전공사` | 3,628 | 7.4억 | `CATEGORY_NORM`엔 `"수전/위생공사"`만 있음 — 슬래시 없는 변형 누락 |
| `기타공사` | 3,212 | 14.4억 | 매핑 불가한 잡항목 (대응 보류) |
| `도기공사` | 1,817 | 4.7억 | **파싱 프롬프트(규칙7)가 표준으로 지정하는 카테고리인데 매핑 테이블에 없었음** |
| `도기,수전` | 1,094 | 2.4억 | 복합 표기 |
| `목공,도어` 등 | 978 | 3.7억 | 복합 표기 |
| `확장공사` | 623 | 3.5억 | 파싱 프롬프트가 표준 지정했는데 매핑 누락 (대응 보류 — 아래 참고) |
| `단가참고`류 | 1,504 | 2.7억 | OCR 변형 다수, 실제 작업 항목이 아닐 가능성 (대응 보류) |
| `철호공사` | 46 | 3,600만 | "창호공사"의 OCR 오타 |

**의미**: `pipeline/reference/parse_estimates.py`의 파싱 프롬프트와 `pipeline/reference/build_rag.py`의
`CATEGORY_NORM`이 서로 다른 시점에 따로 만들어져 어긋나 있었다 — 프롬프트가 LLM에게 "도기공사"를
표준 카테고리로 쓰라고 지시하는데, 그걸 소비하는 매핑 테이블엔 "도기공사"가 아예 없어서
`build_category_costs()`가 조용히 버리고 있었다(`if not norm: continue`).

## 수정한 것

- `수전공사`, `도기공사`, `도기,수전`, `철호공사`를 `CATEGORY_NORM`에 추가 (전부 기존 매핑 패턴과
  일관된 확실한 케이스)
- `normalize_category()`로 쉼표 결합 복합 표기(`"목공,도어"` 등)를 일반적으로 분해해 첫 인식
  토큰을 사용 — 하드코딩 대신 앞으로의 새 조합에도 대응

## 수정하지 않고 남긴 것 (판단 필요)

- **`확장공사`(623건, 3.5억)** — 발코니 확장은 기존 12개 공종(`공종_TO_COST`) 어디에도 맞지 않음.
  억지로 끼워맞추면 또 다른 오분류를 만들 위험이 있어 새 cost 카테고리 신설 여부를 사람이
  판단해야 함
- **`기타공사`(3,212건, 14.4억)** — 정의상 매핑 불가
- **`단가참고`류(1,504건, 2.7억)** — 실제 작업이 아니라 참고용 메모 행일 가능성. 파싱 단계
  (`_remove_aggregate_items()`)에서 애초에 제외됐어야 할 항목일 수 있어, 검증 레이어가 아니라
  파싱 프롬프트 자체를 손봐야 하는 문제일 수 있음

## 결과 (수정 전 → 후)

| 지표 | 수정 전 | 수정 후 |
|---|---:|---:|
| `unknown_category` 위반 건수 | 2,530 | **1,675** (-33.8%) |
| 평균 신뢰도 | 0.55 | **0.70** |
| confidence < 0.7인 사례 | 349건 (61%) | **181건** (32%) |

`door_reclassification` 제안은 3건(233만원)뿐으로 영향이 작았다 — 처음 가설(창호 이질성의
주범)과 달리, 실제로는 `unknown_category` 문제가 훨씬 큰 비중을 차지했다.

## DB 반영 — `scripts/recalculate_category_costs.py`

수정된 `CATEGORY_NORM`/`normalize_category()`로 `estimate_cases`의 `cost_*` 필드를 실제로
재계산해 반영했다. 반영 전 `scripts/backup_estimate_cases.py`로 572건 전체를 로컬 백업
(`backups/estimate_cases_*.json`, gitignore 처리, 임베딩 포함 18.8MB).

**변경 규모**: 572건 중 **468건(82%)의 `cost_*` 필드가 변경됨.**

| 필드 | 변화량 합계 | 신규로 값이 생긴 사례 |
|---|---:|---:|
| **cost_설비** | **14.5억원** | **436건** |
| cost_목공 | 3.8억원 | 79건 |
| cost_철거 | 0.99억원 | 23건 |
| cost_창호 | 0.76억원 | 3건 |
| cost_전기 | 0.20억원 | 8건 |
| cost_타일/가구/도장/도배/바닥/욕실/필름 | 각 수백만 원대 | 0건 (기존 값 소폭 조정) |

**반영 후 재검증**:
- `cost_설비 > 0`인 사례: 약 12건 → **448건** — "수전공사"/"도기공사" 매핑 누락이 사실상
  설비 비용 데이터를 통째로 비워두고 있었다는 게 확인됨. 이번 작업에서 가장 큰 실질 성과.
- `cost_창호` 지역별 격차(서울 vs 경기, 약 6배)는 **거의 그대로** — 예상대로이다. "철호공사"
  OCR 오타 수정은 3건에만 영향을 줬고, 창호의 근본 문제(필름 작업과 전체 샷시 교체가 같은
  필드에 뒤섞임)는 카테고리 매핑 버그가 아니라 서로 다른 서비스가 같은 필드로 집계되는
  구조적 이질성이라 이번 수정 범위 밖이다 — `eval/results/pricing_gap_diagnostic.md`의
  원래 결론("데이터 품질 한계, 근본 해결 안 됨")이 여전히 유효함.

## 한계

- `확장공사`/`기타공사`/`단가참고`류는 여전히 미해결 — 후속 이슈로 분리 필요
- 이 검증 로직은 아직 실제 파싱 파이프라인(`pipeline/reference/parse_estimates.py`)에 연결되지
  않았다 — 지금은 기존 데이터 소급 검증·재계산 용도로만 씀. 앞으로 새로 크롤링·파싱되는
  데이터도 같은 문제를 겪지 않으려면 파싱 파이프라인 자체에 이 검증을 연결해야 함
- 창호 공종의 근본 문제(서로 다른 서비스가 한 필드에 뒤섞임)는 이번 작업으로 해결되지 않음

## 재현 방법

```bash
python -m scripts.validate_existing_cases       # 진단만, DB 변경 없음
python -m scripts.backup_estimate_cases         # DB 변경 전 백업 (backups/, gitignore 처리)
python -m scripts.recalculate_category_costs           # dry-run — 변경될 내용만 미리 확인
python -m scripts.recalculate_category_costs --apply    # 실제 DB 반영
```

