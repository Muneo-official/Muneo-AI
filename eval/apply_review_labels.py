"""
eval/apply_review_labels.py — review_priority.csv에서 사용자가 수정한 label을
labels.csv / pool.json 원본에 (query_id, article_id) 기준으로 병합해 넣는다.
"""

import csv
import json
import pathlib

LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"
POOL_JSON_PATH = pathlib.Path(__file__).parent / "test_inputs" / "pool.json"
REVIEW_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "review_priority.csv"


def _read_csv_rows(path: pathlib.Path) -> list[dict]:
    """Excel이 한글 Windows에서 CSV를 UTF-8이 아니라 cp949(ANSI)로 저장하는 경우가 있어 폴백 처리."""
    for encoding in ("utf-8-sig", "cp949"):
        try:
            return list(csv.DictReader(path.open(encoding=encoding)))
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path}를 utf-8-sig/cp949 어느 쪽으로도 읽지 못했습니다.")


def main() -> None:
    review_rows = _read_csv_rows(REVIEW_CSV_PATH)
    overrides = {(r["query_id"], r["article_id"]): int(r["label"]) for r in review_rows}

    # labels.csv 갱신
    label_rows = _read_csv_rows(LABELS_CSV_PATH)
    changed = 0
    for r in label_rows:
        key = (r["query_id"], r["article_id"])
        if key in overrides and int(r["label"]) != overrides[key]:
            r["label"] = str(overrides[key])
            changed += 1

    with LABELS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=label_rows[0].keys())
        writer.writeheader()
        writer.writerows(label_rows)

    # pool.json도 동일하게 갱신 (retrieval_eval.py가 이걸 읽음)
    pool = json.loads(POOL_JSON_PATH.read_text(encoding="utf-8"))
    for row in pool:
        key = (row["query_id"], row["article_id"])
        if key in overrides:
            row["label"] = overrides[key]

    POOL_JSON_PATH.write_text(json.dumps(pool, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] review_priority.csv {len(review_rows)}건 검토 → labels.csv/pool.json에 {changed}건 반영 (변경분만 카운트)")


if __name__ == "__main__":
    main()
