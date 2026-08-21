"""
eval/flag_review_rows.py — labels.csv 915건 중 사람 판단이 실제로 필요한 행만 추려서
eval/test_inputs/review_priority.csv로 내보낸다.

플래그 기준 (LABELING_GUIDE.md 5번과 동일):
  1. vector_rank 또는 bm25_rank가 1~3위인데 label=0 (검색은 강하게 추천했는데 규칙은 탈락시킨 경우)
  2. 평수 차이가 6~7평(경계)이라 label=0인 경우
  3. label=1인데 공종 겹침이 1개뿐인 경우 (억지로 통과했을 가능성)

나머지 행은 규칙이 이미 명확하다고 보고 손대지 않는다 (suggested_relevant 그대로 신뢰).

"""

import csv
import json
import pathlib

QUERIES_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"
POOL_JSON_PATH = pathlib.Path(__file__).parent / "test_inputs" / "pool.json"
REVIEW_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "review_priority.csv"


def _reasons(query_input: dict, row: dict) -> list[str]:
    reasons = []
    vr, br = row.get("vector_rank"), row.get("bm25_rank")
    size_diff = abs(int(query_input["평수"]) - int(row.get("size_pyeong") or 0))
    overlap = set(query_input.get("공종", [])) & set(row.get("works", []))

    if row["label"] == 0 and ((vr and vr <= 3) or (br and br <= 3)):
        reasons.append("top3_but_zero")
    if row["label"] == 0 and 6 <= size_diff <= 7:
        reasons.append("borderline_size")
    if row["label"] == 1 and len(overlap) == 1:
        reasons.append("weak_overlap_one")
    return reasons


def main() -> None:
    queries = {q["query_id"]: q for q in json.loads(QUERIES_PATH.read_text(encoding="utf-8"))}
    pool = json.loads(POOL_JSON_PATH.read_text(encoding="utf-8"))

    flagged = []
    for row in pool:
        q_input = queries[row["query_id"]]["input"]
        reasons = _reasons(q_input, row)
        if reasons:
            flagged.append((row, reasons))

    with REVIEW_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "query_id", "query_size", "query_region", "query_works",
            "article_id", "region", "size_pyeong", "works",
            "vector_rank", "bm25_rank", "reason", "suggested_relevant", "label",
        ])
        writer.writeheader()
        for row, reasons in flagged:
            qi = queries[row["query_id"]]["input"]
            writer.writerow({
                "query_id": row["query_id"],
                "query_size": qi.get("평수"),
                "query_region": qi.get("지역"),
                "query_works": ",".join(qi.get("공종", [])),
                "article_id": row["article_id"],
                "region": row["region"],
                "size_pyeong": row["size_pyeong"],
                "works": ",".join(row["works"]),
                "vector_rank": row["vector_rank"] or "",
                "bm25_rank": row["bm25_rank"] or "",
                "reason": "+".join(reasons),
                "suggested_relevant": int(row["suggested_relevant"]),
                "label": row["label"],
            })

    print(f"[OK] 전체 {len(pool)}건 중 검토 필요 {len(flagged)}건 → {REVIEW_CSV_PATH}")


if __name__ == "__main__":
    main()
