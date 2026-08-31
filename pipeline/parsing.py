"""LLM 비전 파싱 결과의 후처리·병합 — 그리고 검증 연결.

pipeline/reference/parse_estimates.py의 순수 로직(이미지 다운로드·청크 분할·Anthropic API
호출은 제외, JSON 후처리·병합만)을 이관했다.

이 모듈이 새로 추가하는 것: `merge_and_validate()` — 기존 merge_parsed_results()가 끝나면
바로 pipeline.validators.validate_case()를 돌려서 결과에 `_validation`을 붙인다. 지금까지는
검증이 사후(estimate_cases 적재 후) 소급 실행되는 것뿐이었는데, 이걸 파싱 직후로 당겨서
문제가 생기자마자 드러나게 한다.
"""

import json
import re
from collections import defaultdict
from dataclasses import asdict

from pipeline.validators import validate_case


def _parse_raw(text: str) -> dict:
    """API 응답 텍스트에서 JSON 객체 추출."""
    raw = text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    if start == -1:
        raise json.JSONDecodeError("JSON 객체를 찾을 수 없음", raw, 0)
    obj, _ = json.JSONDecoder().raw_decode(raw, start)
    return obj


def _fix_column_swap(item: dict) -> dict:
    """unit_price 파싱 오류 자동 수정 (열 뒤바뀜, 합계가 단가 열에 들어간 경우 등)."""
    up = item.get("unit_price") or 0
    qty = item.get("quantity") or 1.0
    amt = item.get("amount") or 0

    if up <= 0 or amt <= 0 or qty <= 1:
        return item

    if up > amt:
        expected = amt * qty
        if abs(expected - up) / up < 0.10:
            item = dict(item)
            item["unit_price"] = amt
            item["amount"] = up
        return item

    if up == amt:
        per_unit = amt / qty
        if abs(per_unit - round(per_unit)) < 0.5:
            item = dict(item)
            item["unit_price"] = int(round(per_unit))
        return item

    if up < 1000 and qty >= 1000 and amt > 0 and abs(int(qty) * up - amt) / amt < 0.01:
        item = dict(item)
        item["unit_price"] = int(qty)
        item["quantity"] = float(up)

    return item


def _chunk_dedup_key(item: dict) -> tuple:
    code = item.get("code", "")
    cat = item.get("category", "")
    amt = int(item.get("amount") or 0)
    unit_p = int(item.get("unit_price") or 0)
    desc_pre = item.get("description", "")[:4]
    if code:
        return (code, cat, amt, unit_p)
    return (cat, amt, unit_p, desc_pre)


_AGGREGATE_KEYWORDS = {"합계", "소계", "총계", "공사비합계", "공사합계", "계", "합 계", "소 계"}


def _remove_aggregate_items(line_items: list[dict], total_cost: int = 0) -> list[dict]:
    """집계 행(소계·합계·카테고리 소계) 제거."""
    cleaned = []
    for item in line_items:
        desc = (item.get("description") or "").strip()
        cat = (item.get("category") or "").strip()
        desc_norm = desc.replace(" ", "")
        cat_norm = cat.replace(" ", "")

        if desc_norm in _AGGREGATE_KEYWORDS or any(kw in desc_norm for kw in {"합계", "소계", "총계"}):
            continue
        if cat_norm and desc_norm and desc_norm == cat_norm:
            continue
        if cat_norm and desc_norm and desc_norm == cat_norm + "합계":
            continue
        cleaned.append(item)

    if total_cost > 0:
        line_sum = sum(item.get("amount") or 0 for item in cleaned)
        if line_sum > total_cost * 1.2:
            cleaned = _remove_category_subtotals(cleaned)

    return cleaned


def _remove_category_subtotals(line_items: list[dict]) -> list[dict]:
    """같은 category 내에서 amount ≈ 다른 항목 합인 행(카테고리 소계) 제거."""
    cat_groups: dict[str, list[tuple[int, dict]]] = defaultdict(list)
    for idx, item in enumerate(line_items):
        cat_groups[item.get("category", "")].append((idx, item))

    remove_idxs: set[int] = set()
    for _cat, idx_items in cat_groups.items():
        if len(idx_items) < 3:
            continue
        amounts = [(idx, item.get("amount") or 0) for idx, item in idx_items]
        for i, (idx, amt) in enumerate(amounts):
            if amt == 0:
                continue
            other_sum = sum(a for j, (_, a) in enumerate(amounts) if j != i)
            if other_sum > 0 and abs(amt - other_sum) / max(amt, other_sum) <= 0.05:
                remove_idxs.add(idx)

    return [item for i, item in enumerate(line_items) if i not in remove_idxs]


def _add_consistency_warning(result: dict) -> dict:
    total = int(result.get("total_cost") or 0)
    if total == 0:
        return result
    line_sum = sum(
        int(item.get("amount") or 0)
        for item in result.get("line_items", [])
        if isinstance(item.get("amount"), (int, float))
    )
    error_rate = abs(line_sum - total) / total
    if error_rate > 0.20:
        result = dict(result)
        result["_parse_warning"] = (
            f"합계 불일치: items={line_sum:,} total={total:,} 오차={error_rate * 100:.1f}%"
        )
    return result


def _calc_consistency(r: dict) -> float:
    total = int(r.get("total_cost") or 0)
    if total == 0:
        return float("inf")
    line_sum = sum(
        item.get("amount") or 0
        for item in r.get("line_items", [])
        if isinstance(item.get("amount"), (int, float))
    )
    return abs(line_sum - total) / total


def merge_parsed_results(results: list[dict]) -> dict:
    """여러 이미지 파싱 결과를 하나의 parsed_estimate로 병합.

    총금액이 서로 다른 이미지가 섞여 있으면 독립된 견적서로 판단해 내부 일관성이
    가장 높은 단일 이미지만 사용하고, 같거나 하나에만 있으면 동일 견적서의 여러
    페이지로 보고 중복 제거 병합한다.
    """
    estimate_results = [r for r in results if r.get("is_estimate")]
    if not estimate_results:
        return {}

    if len(estimate_results) == 1:
        r = estimate_results[0]
        total = int(r.get("total_cost") or 0)
        items = [_fix_column_swap(it) for it in r.get("line_items", []) if it.get("amount")]
        items = _remove_aggregate_items(items, total)
        return _add_consistency_warning({"total_cost": total, "line_items": items})

    totals = {int(r.get("total_cost") or 0) for r in estimate_results}
    totals.discard(0)

    if len(totals) >= 2:
        best = min(estimate_results, key=_calc_consistency)
        best_total = int(best.get("total_cost") or 0)
        items = [_fix_column_swap(it) for it in best.get("line_items", []) if it.get("amount")]
        items = _remove_aggregate_items(items, best_total)
        return _add_consistency_warning({"total_cost": best_total, "line_items": items})

    seen = set()
    all_items = []
    max_total = 0
    for r in estimate_results:
        total = int(r.get("total_cost") or 0)
        if total > max_total:
            max_total = total
        for item in r.get("line_items", []):
            if not item.get("amount"):
                continue
            key = (item.get("code", ""), item.get("description", "")[:8])
            if key not in seen:
                seen.add(key)
                all_items.append(item)

    if not all_items:
        return {}
    all_items = [_fix_column_swap(it) for it in all_items]
    all_items = _remove_aggregate_items(all_items, max_total)
    return _add_consistency_warning({"total_cost": max_total, "line_items": all_items})


def merge_and_validate(results: list[dict], size_pyeong: int) -> dict:
    """merge_parsed_results()로 병합한 뒤, 그 자리에서 바로 검증까지 실행해 `_validation`을
    붙인다. 파싱 직후 문제를 드러내기 위한 진입점 — 사후 소급 검증(scripts/validate_existing_cases.py)
    을 기다릴 필요가 없어진다."""
    merged = merge_parsed_results(results)
    if not merged:
        return merged

    result = validate_case(merged, size_pyeong)
    merged["_validation"] = {
        "confidence": result.confidence,
        "issues": [asdict(issue) for issue in result.issues],
        "reclassification_suggestions": result.reclassification_suggestions,
    }
    return merged
