"""
eval/sample_queries.py — retrieval 정량 평가용 쿼리 20~30개 샘플링

estimate_cases 실제 사례를 지역/평수/공종이 고르게 분포하도록 샘플링해서,
각 사례로부터 EstimateEngine.generate()의 입력(inp) 형태의 쿼리를 역으로 구성한다.
(build_query()가 받는 것과 동일한 형식 — retrieve_cases()가 실제로 받는 입력과 일치시키기 위함)

출력: eval/test_inputs/queries.json
    [{"query_id": "q01", "seed_article_id": "845986", "input": {...inp...}}, ...]

"""

import argparse
import asyncio
import json
import pathlib
import random

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from app.domain.estimate_engine import _HAS_TO_WORK, REGION_MAP

load_dotenv()

OUTPUT_PATH = pathlib.Path(__file__).parent / "test_inputs" / "queries.json"

# region(경기/인천/부산/...) → 사용자 입력용 지역 버킷(서울/수도권/지방) 역매핑
_REGION_TO_BUCKET = {
    region: bucket
    for bucket, regions in REGION_MAP.items()
    for region in regions
}

_SIZE_BUCKETS = [(0, 20), (20, 30), (30, 40), (40, 999)]


def _size_bucket(size: int) -> int:
    for i, (lo, hi) in enumerate(_SIZE_BUCKETS):
        if lo <= size < hi:
            return i
    return len(_SIZE_BUCKETS) - 1


def _case_to_query_input(case: dict) -> dict:
    works = [name for key, name in _HAS_TO_WORK.items() if case.get(key) == "true"]
    # _HAS_TO_WORK의 "바닥"/"타일"은 EstimateEngine이 쓰는 공종 이름(마루/욕실 등)과 다르므로 매핑
    work_map = {"바닥": "마루", "타일": "욕실", "조명": "전기/조명", "전기": "전기/조명"}
    공종 = sorted({work_map.get(w, w) for w in works}) or ["도배"]

    return {
        "공종": 공종,
        "시공범위": "부분",
        "공간유형": "아파트",
        "평수": int(case.get("size_pyeong") or 25),
        "방개수": 3,
        "지역": _REGION_TO_BUCKET.get(case.get("region"), "지방"),
        "건물연식": "10~20년",
        "자재등급": case.get("material_grade", "중급"),
        "철거여부": "모름",
        "층수": 5,
        "엘리베이터": "있음",
        "트럭접근": "가능",
        "거주중공사": "공실",
        "공사시기": "미정",
    }


async def sample(n: int, seed: int) -> list[dict]:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    col = client[settings.mongo_db_name]["estimate_cases"]

    cases = [
        c async for c in col.find(
            # size_pyeong > 100은 원본 데이터 이슈로 article_id가 잘못 들어간 케이스가 섞여있어 제외
            # (예: article_id=878955 문서의 size_pyeong=877935 — 실제 제목엔 "34평")
            {"region": {"$exists": True}, "size_pyeong": {"$gt": 0, "$lt": 100}, "total_cost": {"$gt": 0}},
            {"embedding": 0},
        )
    ]
    client.close()

    # (지역버킷, 평수버킷) 조합별로 그룹핑 후 라운드로빈으로 뽑아 분포를 고르게 함
    groups: dict[tuple, list[dict]] = {}
    for c in cases:
        key = (_REGION_TO_BUCKET.get(c.get("region"), "지방"), _size_bucket(int(c["size_pyeong"])))
        groups.setdefault(key, []).append(c)

    rng = random.Random(seed)
    for g in groups.values():
        rng.shuffle(g)

    group_keys = list(groups.keys())
    rng.shuffle(group_keys)

    picked: list[dict] = []
    idx = 0
    while len(picked) < n and group_keys:
        key = group_keys[idx % len(group_keys)]
        if groups[key]:
            picked.append(groups[key].pop())
        else:
            group_keys.remove(key)
            continue
        idx += 1
        if idx > n * 10:  # 안전장치
            break

    queries = []
    for i, case in enumerate(picked, start=1):
        queries.append({
            "query_id": f"q{i:02d}",
            "seed_article_id": case.get("article_id"),
            "input": _case_to_query_input(case),
        })
    return queries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=24)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    queries = asyncio.run(sample(args.n, args.seed))
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(queries, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] {len(queries)}개 쿼리 샘플링 완료 → {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
