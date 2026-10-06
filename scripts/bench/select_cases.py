"""
scripts/bench/select_cases.py — estimate_data/의 실제 크롤링 이미지로 벤치마크 테스트셋(S1~S4)을 고른다.

API 호출 없이 이미지 크기만 읽어 청크 수(= 직렬 Vision 호출 수)를 계산하고, 청크 수 구간별로
케이스를 뽑는다. 크롤링 데이터엔 "어느 이미지가 견적서인가" 라벨이 없어서 세로로 긴 이미지
(열린 견적서 표 캡처)만 후보로 삼는다 — 게시글째 묶으면 현장 사진·인물 사진이 섞여 들어갔다.
그래도 뽑힌 이미지는 사람이 직접 열어 견적서가 맞는지 확인한 뒤 확정한다.
아니면 --exclude로 빼고 다시 돌린다.

  S1: 이미지 1장, 1청크
  S2: 이미지 1장, 2~3청크
  S3: 견적서 이미지 여러 장, 총 3~5청크
  S4: 견적서 이미지 여러 장, 총 6청크 이상 (최악 케이스)

여러 장 케이스는 서로 다른 게시글의 견적서 이미지를 조합한다. 응답시간을 좌우하는 건
이미지·청크 수라서 같은 견적서의 여러 페이지일 필요는 없다(이미지 간 중복 제거 로직만 덜 탄다).

사용법:
  python -m scripts.bench.select_cases
  python -m scripts.bench.select_cases --exclude 790708,824399 --seed 7
"""

import argparse
import json
import pathlib
import random
import re
import sys

from PIL import Image

from app.domain.risk_input_guard import MAX_CHUNKS_PER_IMAGE, MAX_CHUNKS_PER_REQUEST, MAX_PYEONG, MIN_PYEONG
from pipeline.crawl_filter import is_boilerplate
from scripts.bench.common import DEFAULT_CASES_FILE, count_chunks

for _stream in (sys.stdout, sys.stderr):
    if _stream.encoding and _stream.encoding.lower() != "utf-8":
        _stream.reconfigure(encoding="utf-8")

DATA_DIR = pathlib.Path("estimate_data")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}
MIN_SIDE_PX = 500  # 로고·아이콘·배너 같은 작은 이미지는 견적서 표일 수 없음
MIN_ASPECT = 2.0   # 세로/가로 — 열린 견적서 표 캡처는 세로로 길다 (현장 사진·배너 제외)
MAX_IMAGES_PER_CASE = 4

BUCKETS = {
    "S1": {"desc": "이미지 1장, 1청크", "multi": False, "chunks": (1, 1)},
    "S2": {"desc": "이미지 1장, 2~3청크", "multi": False, "chunks": (2, 3)},
    "S3": {"desc": "이미지 여러 장, 총 3~5청크", "multi": True, "chunks": (3, 5)},
    # 위쪽 끝은 요청당 조각 상한 — 넘는 요청은 Vision까지 가지 못하고 422로 끝난다
    "S4": {"desc": "이미지 여러 장, 총 6청크 이상", "multi": True, "chunks": (6, MAX_CHUNKS_PER_REQUEST)},
}


def _form_region(folder_region: str) -> str:
    """수집 폴더의 지역 이름(경기, 부산 …)을 리스크 진단 폼이 받는 값으로 바꾼다."""
    if folder_region == "서울":
        return "서울"
    return "수도권" if folder_region in ("경기", "인천") else "지방"


def _page_order(path: pathlib.Path) -> tuple[int, str]:
    """123_2.png < 123_10.png — 사전순이면 페이지 순서가 뒤섞인다."""
    m = re.search(r"_(\d+)$", path.stem)
    return (int(m.group(1)) if m else 0, path.name)


def _scan_article(article_dir: pathlib.Path) -> dict | None:
    meta_path = article_dir / f"{article_dir.name}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        meta = {}  # 메타 JSON이 없거나 깨진 게시글도 있다 — 지역은 폴더명, 평수는 기본값으로
    images = []
    for p in sorted(article_dir.iterdir(), key=_page_order):
        if p.suffix.lower() not in IMAGE_SUFFIXES or is_boilerplate(str(p)):
            continue
        try:
            with Image.open(p) as img:
                w, h = img.size
        except OSError:
            continue
        if min(w, h) < MIN_SIDE_PX or h / w < MIN_ASPECT or count_chunks(w, h) > MAX_CHUNKS_PER_IMAGE:
            continue
        images.append({"path": p.as_posix(), "width": w, "height": h, "chunks": count_chunks(w, h)})
    if not images:
        return None
    return {
        "article_id": article_dir.name,
        "region": meta.get("region") or article_dir.parent.name,
        "pyeong": meta.get("size_pyeong") or 30,
        "images": images,
    }


def _single_candidates(articles: list[dict], bucket: dict) -> list[dict]:
    lo, hi = bucket["chunks"]
    return [
        {**a, "images": [img]}
        for a in articles
        for img in a["images"]
        if lo <= img["chunks"] <= hi
    ]


def _multi_candidates(articles: list[dict], bucket: dict, rng: random.Random, n: int) -> list[dict]:
    """서로 다른 게시글의 견적서 이미지를 청크 합이 구간에 들 때까지 이어 붙인다."""
    lo, hi = bucket["chunks"]
    pool = [(a, img) for a in articles for img in a["images"]]
    out = []
    for _ in range(n * 20):
        if len(out) >= n:
            break
        rng.shuffle(pool)
        picked, used, total = [], set(), 0
        for a, img in pool:
            if a["article_id"] in used or total + img["chunks"] > hi:
                continue
            picked.append((a, img))
            used.add(a["article_id"])
            total += img["chunks"]
            if (total >= lo and len(picked) >= 2) or len(picked) == MAX_IMAGES_PER_CASE:
                break
        if len(picked) >= 2 and lo <= total <= hi:
            first = picked[0][0]
            out.append({
                **first,
                "article_id": "+".join(a["article_id"] for a, _ in picked),
                "images": [img for _, img in picked],
            })
    return out


def _to_case(case_id: str, desc: str, a: dict) -> dict:
    return {
        "id": case_id,
        "description": desc,
        "article_id": a["article_id"],
        "images": [i["path"] for i in a["images"]],
        "chunk_counts": [i["chunks"] for i in a["images"]],
        "total_chunks": sum(i["chunks"] for i in a["images"]),
        # 폼 값은 결과(가격 체크 비교 사례)에만 영향 — 케이스 간 비교가 되도록 공간 조건은 고정한다
        "form": {
            "space_type": "아파트",
            # 수집 메타의 평수에는 깨진 값이 섞여 있다 — 폼이 받는 범위 안으로
            "pyeong": min(max(int(a["pyeong"]), MIN_PYEONG), MAX_PYEONG),
            "room_count": 3,
            "floor": 10,
            "elevator": True,
            "region": _form_region(a["region"]),
            "building_age": "10~20년",
            "company_name": "벤치마크",
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, default=DEFAULT_CASES_FILE)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--exclude", default="", help="제외할 article_id (쉼표 구분) — 견적서가 아닌 이미지였던 경우")
    parser.add_argument("--alternates", type=int, default=3, help="케이스별 예비 후보 수")
    args = parser.parse_args()

    excluded = {x.strip() for x in args.exclude.split(",") if x.strip()}
    article_dirs = [d for d in DATA_DIR.glob("*/*") if d.is_dir() and d.name not in excluded]
    articles = [a for d in article_dirs if (a := _scan_article(d))]
    print(f"게시글 {len(article_dirs)}개 스캔 → 후보 이미지가 있는 게시글 {len(articles)}개")

    rng = random.Random(args.seed)
    cases, alternates = [], {}
    for case_id, bucket in BUCKETS.items():
        if bucket["multi"]:
            pool = _multi_candidates(articles, bucket, rng, 1 + args.alternates)
        else:
            pool = _single_candidates(articles, bucket)
            rng.shuffle(pool)
        if not pool:
            print(f"[WARN] {case_id}({bucket['desc']}) 후보 없음")
            continue
        cases.append(_to_case(case_id, bucket["desc"], pool[0]))
        alternates[case_id] = [_to_case(case_id, bucket["desc"], a) for a in pool[1 : 1 + args.alternates]]
        print(f"{case_id} ({bucket['desc']}): 후보 {len(pool)}개 → article {pool[0]['article_id']}, "
              f"청크 {cases[-1]['chunk_counts']}")
        for path in cases[-1]["images"]:
            print(f"    {path}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"seed": args.seed, "cases": cases, "alternates": alternates}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n저장: {args.out}")
    print("다음: 위 이미지를 열어 실제 견적서인지 확인하고, 아니면 --exclude로 빼고 다시 실행")


if __name__ == "__main__":
    main()
