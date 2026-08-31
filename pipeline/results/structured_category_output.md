# 구조화 출력(tool use)으로 category enum 강제 — 결과

## 배경

Phase 1(`pipeline/prompts.py`)에서 프롬프트 규칙으로 카테고리 표준화를 유도했지만, 목표했던
"단가참고" 재분류 버그의 직접 재현·검증에는 실패했다(`pipeline/results/prompt_category_fix.md`).
결론에서 짚었듯 — 자유 텍스트 출력인 이상 프롬프트 지시를 아무리 정교하게 써도 모델이 이탈할
여지가 항상 남는다. 이번 단계는 그 여지 자체를 API 차원에서 없애는 시도다.

## 구현

- `pipeline/categories.py`: `NORMALIZED_CATEGORIES` 추가 — `CATEGORY_NORM`이 최종적으로
  매핑하는 14개 정규화 카테고리(가구, 공과잡비, 도배, 도장, 목공, 바닥, 설비, 욕실, 전기,
  창호, 철거, 타일, 필름, 확장)를 단일 소스로 도출
- `pipeline/tool_schema.py` 신규: Anthropic tool use 정의(`ESTIMATE_TOOL`) — `line_items[].category`
  에 `NORMALIZED_CATEGORIES`를 JSON schema `enum`으로 직접 걸었다. 카테고리 표준화 규칙
  (기존 규칙 7/7-1)은 enum이 구조적으로 대신하므로 프롬프트(`TOOL_USE_INSTRUCTIONS`)에서
  뺐고, 표 선택·집계행 제외·total_cost 산정·열 뒤바뀜·자기검증 규칙만 남겼다

## 실제 검증 — Vision API tool use 실호출

같은 두 실제 크롤링 이미지(850258, 850833, 청크 분할 적용)로 `tool_choice`를 강제해 호출.

| 이미지 | 청크 수 | 항목 수 | enum 밖 카테고리 |
|---|---:|---:|---:|
| 850258_1.png | 2 | 69 | **0** |
| 850833_1.png | 1 | 57 | **6** |
| 합계 | | 126 | **6 (4.8%)** |

**850258**: "기타공사"였던 항목들이 정확히 **"공과잡비"**로 분류됨 — Phase 1에서 신설한
카테고리가 tool use 환경에서도 의도대로 작동하는 것을 확인. 69개 전부 14개 enum 안.

**850833**: `"조명"`이라는 enum에 없는 값이 6건 출력됨(`CATEGORY_NORM`에서 "조명공사"는
"전기"로 매핑되지 "조명"이라는 별도 버킷은 없음). **tool use의 JSON schema enum 제약이
100% 강제되는 게 아님을 실측으로 확인** — Anthropic API가 구조화 출력을 강하게 유도하지만
드물게 enum 밖 값이 새어나갈 수 있다.

## 결론

- 자유 텍스트 대비 확실한 개선: 카테고리 변형이 사실상 무한했던 이전 방식과 달리, 126건 중
  120건(95.2%)이 정확히 14개 정규화 카테고리 중 하나로 바로 나옴 — `normalize_category()`를
  통한 사후 매핑이 대부분의 경우 더 이상 필요 없어짐
- **완전한 원천 차단은 아니다** — enum 제약을 우회하는 사례가 실측으로 확인됐다(4.8%)
- 따라서 `pipeline/validators.py`의 사후 검증(`unknown_category`)은 tool use 도입 이후에도
  **여전히 필요한 안전망**이다 — "구조화 출력을 쓰니 검증이 필요 없어진다"는 결론은 틀렸다는
  게 실측으로 확인된 셈. 두 계층(생성 시점 제약 + 사후 검증)을 같이 두는 게 맞다

## 한계

- 표본이 2개 이미지(126개 항목)로 작다 — enum 이탈률(4.8%)이 통계적으로 안정된 수치인지는
  더 큰 표본으로 확인이 필요하다
- `merge_and_validate()`(`pipeline/parsing.py`)가 아직 tool use 경로와 연결되지 않았다 —
  이번 단계는 tool 정의와 실제 호출 검증까지만, 파싱 파이프라인 전체 배선은 3단계(Vision
  API 호출 코드를 `pipeline/`에 정식 이관)에서 진행

## 재현 방법

Tool 정의 자체는 API 호출 없이 확인 가능:
```bash
python -c "from pipeline.tool_schema import ESTIMATE_TOOL; import json; print(json.dumps(ESTIMATE_TOOL, ensure_ascii=False, indent=2))"
python -m pytest tests/test_pipeline_tool_schema.py -v
```
