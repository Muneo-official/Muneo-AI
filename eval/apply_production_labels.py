"""eval/label_production_candidates.py가 낸 production_review.csv의 라벨을
labels.csv에 병합한다 (기존 (query_id, article_id) 라벨은 건드리지 않고, 신규만 추가).

pool.json은 안 건드린다 — 이 후보들은 필터 없는 top-20 pool 소속이 아니라
vector_rank/bm25_rank가 없어서 eval.retrieval_eval(vector-vs-hybrid 비교)에는 안 쓰인다.
production 경로 자체의 precision은 eval/production_retrieval_eval.py로 별도 계산한다.

실행: python -m eval.apply_production_labels
      python -m eval.apply_production_labels --review-file eval/test_inputs/rerank_ablation_review.csv
"""

import argparse
import csv
import pathlib

LABELS_CSV_PATH = pathlib.Path(__file__).parent / "test_inputs" / "labels.csv"
DEFAULT_REVIEW_PATH = pathlib.Path(__file__).parent / "test_inputs" / "production_review.csv"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-file", type=str, default=str(DEFAULT_REVIEW_PATH))
    args = parser.parse_args()
    review_path = pathlib.Path(args.review_file)

    existing_rows = []
    existing_keys = set()
    fieldnames = [
        "query_id", "query_size", "query_region", "query_works",
        "article_id", "region", "size_pyeong", "works",
        "vector_rank", "bm25_rank", "suggested_relevant", "label",
    ]
    if LABELS_CSV_PATH.exists():
        with LABELS_CSV_PATH.open(encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames or fieldnames
            for row in reader:
                existing_rows.append(row)
                existing_keys.add((row["query_id"], row["article_id"]))

    raw = review_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 엑셀로 열었다가 저장하면 Windows-ANSI(cp949)로 바뀌는 경우가 흔하다.
        text = raw.decode("cp949")

    added = 0
    for row in csv.DictReader(text.splitlines()):
        key = (row["query_id"], row["article_id"])
        if key in existing_keys:
            continue
        new_row = {k: row.get(k, "") for k in fieldnames}
        new_row["vector_rank"] = ""
        new_row["bm25_rank"] = ""
        existing_rows.append(new_row)
        existing_keys.add(key)
        added += 1

    with LABELS_CSV_PATH.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing_rows)

    print(f"[OK] {review_path.name} {added}건 신규 반영 -> {LABELS_CSV_PATH} (총 {len(existing_rows)}건)")


if __name__ == "__main__":
    main()
