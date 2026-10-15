"""LLM 비전 파싱 결과의 후처리·병합 — 그리고 검증 연결.

pipeline/reference/parse_estimates.py의 순수 로직(이미지 다운로드·청크 분할·Anthropic API
호출은 제외, JSON 후처리·병합만)을 이관했다.

이 모듈이 새로 추가하는 것: `merge_and_validate()` — 기존 merge_parsed_results()가 끝나면
바로 pipeline.validators.validate_case()를 돌려서 결과에 `_validation`을 붙인다. 지금까지는
검증이 사후(estimate_cases 적재 후) 소급 실행되는 것뿐이었는데, 이걸 파싱 직후로 당겨서
문제가 생기자마자 드러나게 한다.
"""

from collections import Counter, defaultdict
from dataclasses import asdict

from pipeline.validators import validate_case


def _safe_int(value, default: int = 0) -> int:
    """tool_schema.py가 total_cost/amount를 integer로 강제해도, 모델이 가끔 스키마를
    벗어난 값("<UNKNOWN>" 등)을 낼 수 있다 — risk_detector 실제 이미지 배치 테스트에서
    total_cost="<UNKNOWN>"으로 int() 캐스팅이 그대로 크래시하는 걸 발견해 추가했다."""
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


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
    """청크가 겹친 구간에서 두 번 읽힌 같은 행을 가리는 열쇠.

    코드가 있는 행은 공종(category)을 넣지 않는다. 같은 행이 청크마다 다른 공종으로 읽히는 일이 있어서(싱크볼이 한
    청크에서는 가구, 다른 청크에서는 설비), 공종을 넣으면 그 행이 두 공종에 하나씩 남는다 — 공종 금액의 합이
    총금액보다 커진다. 코드가 행을 가려 주므로 공종 없이도 서로 다른 행이 합쳐지지 않는다.

    코드가 없는 행은 공종을 남긴다. 코드 없이 (금액, 단가, 품명 앞 글자)만으로 가리면 목공의 "인건비 300,000"과
    타일의 "인건비 300,000"이 한 줄로 합쳐져, 뒤쪽 공종의 금액이 사라진다.
    """
    code = item.get("code", "")
    amt = _safe_int(item.get("amount"))
    unit_p = _safe_int(item.get("unit_price"))
    desc_pre = item.get("description", "")[:4]
    if code:
        return (code, amt, unit_p)
    return (item.get("category", ""), amt, unit_p, desc_pre)


def _row_key(item: dict) -> tuple:
    if item.get("amount"):
        return _chunk_dedup_key(item)
    return ("금액 없음", item.get("code", ""), item.get("description", ""))


def _aligned_overlap(previous_rows: list[dict], rows: list[dict]) -> int:
    """앞 청크의 끝 k줄과 이 청크의 앞 k줄이 금액·단가가 차례로 같은 가장 긴 k(없으면 0).

    겹친 구간은 앞 청크의 맨 끝, 다음 청크의 맨 앞에 나온다. 그 줄들은 공종이나 품명이 청크마다 다르게 읽힐 수
    있다 — 다음 청크에서는 구분 이름이 잘려 보이지 않아 가구 줄을 "확장"으로 읽고, "소계"를 "소 계 (확장포함)"로
    옮긴다. 그래서 위치와 금액·단가로 맞춘다. 경계에서 잘린 줄은 한쪽에만 나오지만 바깥 끝에 있어 맞추기를 깨지 않는다.
    """
    def key(item: dict) -> tuple:
        amount = _safe_int(item.get("amount"))
        if not amount:
            return ("금액 없음", (item.get("description") or "").replace(" ", ""))
        return (amount, _safe_int(item.get("unit_price")))

    for k in range(min(len(previous_rows), len(rows)), 0, -1):
        if [key(i) for i in previous_rows[-k:]] == [key(i) for i in rows[:k]]:
            return k
    return 0


def _drop_overlap(previous_rows: list[dict], rows: list[dict]) -> list[dict]:
    """이 청크의 줄 중 바로 앞 청크와 겹친 구간에서 다시 읽힌 줄을 뺀다.

    위치로 맞으면(_aligned_overlap) 앞 k줄을 뺀다. 한 줄만 맞는 것은 우연일 수 있어 두 줄 이상일 때만 믿는다(겹침
    200px에는 보통 대여섯 줄이 들어간다). 위치로 안 맞으면(한쪽 청크에서 금액을 잘못 읽은 경우 등) 열쇠로 가린다 —
    열쇠가 같은 줄이 앞 청크에 n번 있었으면 이 청크의 처음 n번을 뺀다.

    어느 쪽이든 같은 청크 안의 줄끼리는 지우지 않는다. 예전에는 지워서 "식대 15,000"처럼 공종·금액·단가·품명 앞
    4글자가 같은 전기공사 줄과 조명공사 줄이 한 줄로 합쳐졌고, 리스크 진단이 맞는 소계를 "품목 합과 다름"으로 지적했다.
    """
    overlap = _aligned_overlap(previous_rows, rows)
    if overlap >= 2:
        return rows[overlap:]
    in_previous = Counter(_row_key(i) for i in previous_rows)
    in_this: Counter = Counter()
    kept = []
    for item in rows:
        in_this[_row_key(item)] += 1
        if in_this[_row_key(item)] > in_previous[_row_key(item)]:
            kept.append(item)
    return kept


_AGGREGATE_KEYWORDS = {"합계", "소계", "총계", "공사비합계", "공사합계", "계", "합 계", "소 계"}


def is_subtotal_row(item: dict) -> bool:
    """리스크 진단의 파싱이 품목과 함께 내는 소계 행인지.

    지시문은 description을 "소계"로 적게 하지만, 모델이 "소 계 (확장, 가벽 포함)"처럼 견적서에 적힌 그대로 옮기는
    일이 있다. 정확히 "소계"만 보면 그 행이 집계 행으로 지워져, 그 구분의 품목이 앞 구분의 소계 밑으로 들어간다.
    """
    return (item.get("description") or "").replace(" ", "").startswith("소계")


def _remove_aggregate_items(line_items: list[dict], total_cost: int = 0, keep_subtotals: bool = False) -> list[dict]:
    """집계 행(소계·합계·카테고리 소계) 제거. keep_subtotals면 "소계" 행은 남긴다(리스크 진단의 검산용)."""
    cleaned = []
    for item in line_items:
        desc = (item.get("description") or "").strip()
        cat = (item.get("category") or "").strip()
        desc_norm = desc.replace(" ", "")
        cat_norm = cat.replace(" ", "")

        if keep_subtotals and is_subtotal_row(item):
            cleaned.append(item)
            continue
        if desc_norm in _AGGREGATE_KEYWORDS or any(kw in desc_norm for kw in {"합계", "소계", "총계"}):
            continue
        if cat_norm and desc_norm and desc_norm == cat_norm:
            continue
        if cat_norm and desc_norm and desc_norm == cat_norm + "합계":
            continue
        cleaned.append(item)

    if total_cost > 0 and not keep_subtotals:
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
    total = _safe_int(result.get("total_cost"))
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
    total = _safe_int(r.get("total_cost"))
    if total == 0:
        return float("inf")
    line_sum = sum(
        item.get("amount") or 0
        for item in r.get("line_items", [])
        if isinstance(item.get("amount"), (int, float))
    )
    return abs(line_sum - total) / total


def merge_chunk_results(chunk_results: list[dict], for_risk: bool = False) -> dict:
    """세로로 길어 여러 청크로 쪼개 파싱한 **같은 이미지**의 결과를 하나로 합친다.

    merge_parsed_results()는 서로 다른 이미지(페이지)를 합치는 함수라 총금액이 다르면
    "독립된 견적서가 섞였다"고 판단해 하나만 골라버린다 — 청크는 애초에 같은 이미지의
    일부라 그 판단이 적용되면 안 된다. 대신 청크가 겹친 구간에서 두 번 읽힌 줄만 지우고,
    total_cost는 청크 중 가장 큰 값(합계 행이 찍힌 청크)을 쓴다.

    겹친 구간은 바로 앞 청크와만 생기므로 각 청크를 바로 앞 청크와만 대 본다(_drop_overlap).

    for_risk: 리스크 진단용. 금액이 없는 행("별도", "협의")과 소계 행을 버리지 않는다 — 앞의 것은 총액에 들어가지
    않은 비용이고, 뒤의 것은 소계 검산에 쓴다. 수집(코퍼스)에서는 둘 다 버린다.
    """
    previous_rows: list[dict] = []
    all_items = []
    max_total = 0

    for r in chunk_results:
        if not r.get("is_estimate"):
            previous_rows = []  # 앞뒤 청크가 이어지지 않으므로 겹친 구간도 없다
            continue
        total = _safe_int(r.get("total_cost"))
        if total > max_total:
            max_total = total
        rows = [
            item for item in r.get("line_items", [])
            if item.get("amount") or (for_risk and (item.get("description") or "").strip())
        ]
        all_items.extend(_drop_overlap(previous_rows, rows))
        previous_rows = rows

    if not all_items:
        return {"is_estimate": False}
    all_items = [_fix_column_swap(it) for it in all_items]
    all_items = _remove_aggregate_items(all_items, max_total, keep_subtotals=for_risk)
    result = {"is_estimate": True, "total_cost": max_total, "line_items": all_items}
    return _add_consistency_warning(result)


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
        total = _safe_int(r.get("total_cost"))
        items = [_fix_column_swap(it) for it in r.get("line_items", []) if it.get("amount")]
        items = _remove_aggregate_items(items, total)
        return _add_consistency_warning({"total_cost": total, "line_items": items})

    totals = {_safe_int(r.get("total_cost")) for r in estimate_results}
    totals.discard(0)

    if len(totals) >= 2:
        best = min(estimate_results, key=_calc_consistency)
        best_total = _safe_int(best.get("total_cost"))
        items = [_fix_column_swap(it) for it in best.get("line_items", []) if it.get("amount")]
        items = _remove_aggregate_items(items, best_total)
        return _add_consistency_warning({"total_cost": best_total, "line_items": items})

    seen = set()
    all_items = []
    max_total = 0
    for r in estimate_results:
        total = _safe_int(r.get("total_cost"))
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
