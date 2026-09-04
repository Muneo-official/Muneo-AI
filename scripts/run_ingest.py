"""estimate_data/ 아래 크롤링된 article 폴더를 전부 순회해 파싱·검증·저장한다.

pipeline/ingest.py의 process_article()/finalize_record()/route_and_save()로 파싱부터
Mongo 저장까지 끝나는데, 이 스크립트는 그 함수들을 여러 article 폴더에 대해 반복
호출해주는 역할만 한다 — 실제 파싱/검증/집계 로직은 전부 pipeline/ 쪽에 있다.

기본은 배치 모드다 (Anthropic Batches API, 실시간 대비 약 50% 저렴) — 물량이 많을 때
비용을 아끼기 위함. 배치는 제출 후 결과가 나올 때까지 시간이 걸려서(보통 수십 분~수
시간) submit/status/collect 세 단계로 나뉘고, 진행 상태는 estimate_data/.batch_state.json
에 저장해 프로세스를 껐다 켜도 이어갈 수 있게 했다.

이미 estimate_cases/review_queue 둘 중 하나에라도 저장된 article_id는 기본적으로
건너뛴다 — 재실행해도 이미 처리한 건에 API를 또 호출해 비용이 중복 발생하지 않게
하기 위함. 재파싱하고 싶으면 --force를 준다.

실행 (배치, 기본):
    python -m scripts.run_ingest submit --dry-run   # 대상만 확인, 제출 안 함
    python -m scripts.run_ingest submit --limit 20  # 배치 제출
    python -m scripts.run_ingest status              # 진행 상황 확인
    python -m scripts.run_ingest collect              # 완료되면 결과 수집·저장

실행 (실시간, 소량 즉시 확인용):
    python -m scripts.run_ingest realtime --limit 5
    python -m scripts.run_ingest realtime --force
"""

import argparse
import asyncio
import json
import pathlib
from dataclasses import asdict

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings
from pipeline.batch_client import (
    RequestMeta,
    build_batch_requests,
    check_batches_status,
    collect_batch_results,
    submit_batches,
)
from pipeline.ingest import finalize_record, ingest_article, load_article_record, route_and_save
from pipeline.parsing import merge_chunk_results
from pipeline.vision_client import get_client

load_dotenv()

STATE_FILE_NAME = ".batch_state.json"


def _article_dirs(base_dir: pathlib.Path):
    """{base_dir}/{지역}/{article_id}/{article_id}.json이 있는 폴더만 골라낸다."""
    for region_dir in sorted(base_dir.iterdir()):
        if not region_dir.is_dir():
            continue
        for article_dir in sorted(region_dir.iterdir()):
            if not article_dir.is_dir():
                continue
            if (article_dir / f"{article_dir.name}.json").exists():
                yield article_dir


async def _already_ingested_ids(cases_col, queue_col) -> set[str]:
    ids: set[str] = set()
    async for doc in cases_col.find({}, {"article_id": 1}):
        ids.add(doc["article_id"])
    async for doc in queue_col.find({}, {"article_id": 1}):
        ids.add(doc["article_id"])
    return ids


def _get_collections():
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]
    return client, db["estimate_cases"], db["review_queue"]


async def _select_new_dirs(base_dir: str, limit: int | None, force: bool):
    all_dirs = list(_article_dirs(pathlib.Path(base_dir)))
    client, cases_col, queue_col = _get_collections()

    if force:
        dirs = all_dirs
        skipped = 0
    else:
        done_ids = await _already_ingested_ids(cases_col, queue_col)
        dirs = [d for d in all_dirs if d.name not in done_ids]
        skipped = len(all_dirs) - len(dirs)

    if limit is not None:
        dirs = dirs[:limit]

    print(f"[INFO] 전체 article 폴더: {len(all_dirs)}개, 이미 처리됨(스킵): {skipped}개, 이번 대상: {len(dirs)}개")
    return client, cases_col, queue_col, dirs


def _state_path(base_dir: str) -> pathlib.Path:
    return pathlib.Path(base_dir) / STATE_FILE_NAME


def _save_state(base_dir: str, batches: list[dict], meta: dict[str, RequestMeta], article_dirs: dict[str, str]):
    state = {
        "batches": batches,
        "meta": {cid: asdict(m) for cid, m in meta.items()},
        "article_dirs": article_dirs,
    }
    _state_path(base_dir).write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_state(base_dir: str) -> dict:
    path = _state_path(base_dir)
    if not path.exists():
        raise SystemExit(f"[ERR] 진행 중인 배치 상태 파일이 없음: {path} — 먼저 submit을 실행할 것")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["meta"] = {cid: RequestMeta(**m) for cid, m in raw["meta"].items()}
    return raw


async def cmd_submit(base_dir: str, limit: int | None, force: bool, dry_run: bool) -> None:
    client, cases_col, queue_col, dirs = await _select_new_dirs(base_dir, limit, force)

    articles: list[tuple[str, list[str]]] = []
    article_dirs: dict[str, str] = {}
    for d in dirs:
        record = load_article_record(str(d))
        image_paths = record.get("local_images") or []
        articles.append((d.name, image_paths))
        article_dirs[d.name] = str(d)

    requests, meta = build_batch_requests(articles)
    print(f"[INFO] 생성된 요청(이미지 청크 단위): {len(requests)}개 (보일러플레이트는 이미 제외됨)")

    # 이미지가 아예 없거나 전부 보일러플레이트라 요청이 하나도 안 나온 article은
    # 배치에 실을 게 없다 — 배치를 기다릴 필요 없이 바로 review_queue로 보낸다.
    # (여기서 처리 안 하면 배치 결과에도 안 나타나서 조용히 유실된다.)
    has_request = {m.article_id for m in meta.values()}
    no_request_ids = [aid for aid in article_dirs if aid not in has_request]
    if no_request_ids:
        print(f"[INFO] 파싱할 이미지 없음(review_queue 즉시 이동 대상): {len(no_request_ids)}개")

    if dry_run:
        client.close()
        return

    if no_request_ids:
        for article_id in no_request_ids:
            article_dir = article_dirs[article_id]
            record = load_article_record(article_dir)
            record = finalize_record(record, [])
            destination = await route_and_save(record, article_dir, cases_col, queue_col)
            print(f"{article_id} -> {destination} (이미지 없음)")

    if not requests:
        print("[INFO] 배치로 제출할 요청 없음")
        client.close()
        return

    anthropic_client = get_client()
    batches = submit_batches(anthropic_client, requests)
    _save_state(base_dir, batches, meta, {aid: d for aid, d in article_dirs.items() if aid in has_request})
    print(f"[OK] {len(batches)}개 배치 제출 완료 -> {_state_path(base_dir)}")
    for b in batches:
        print(f"  - batch_id={b['batch_id']} (요청 {len(b['custom_ids'])}개)")
    client.close()


def cmd_status(base_dir: str) -> None:
    state = _load_state(base_dir)
    anthropic_client = get_client()
    summary = check_batches_status(anthropic_client, state["batches"])
    for s in summary["batches"]:
        print(f"  - {s['batch_id']}: {s['status']} (성공 {s['succeeded']}, 실패 {s['errored']}, 진행중 {s['processing']})")
    print(f"[요약] 성공 {summary['succeeded']}, 실패 {summary['errored']}, 진행중 {summary['processing']}")
    print("전체 완료됨" if summary["all_ended"] else "아직 진행 중 — 나중에 다시 확인할 것")


async def cmd_collect(base_dir: str) -> None:
    state = _load_state(base_dir)
    anthropic_client = get_client()

    summary = check_batches_status(anthropic_client, state["batches"])
    if not summary["all_ended"]:
        print("[WARN] 아직 진행 중인 배치가 있음 — status로 먼저 확인할 것")
        return

    results = collect_batch_results(anthropic_client, state["batches"], state["meta"])
    client, cases_col, queue_col = _get_collections()

    counts = {"estimate_cases": 0, "review_queue": 0}
    failed = []

    # 요청 자체가 없었던(이미지가 아예 없거나 전부 보일러플레이트였던) article은
    # results에 안 나타난다 — 실시간 모드(process_article)처럼 빈 파싱 결과로
    # review_queue에 명시적으로 보내야 조용히 유실되지 않는다.
    for article_id, article_dir in state["article_dirs"].items():
        images = results.get(article_id, {})
        try:
            per_image_results = []
            for image_index in sorted(images.keys()):
                chunk_results = [images[image_index][c] for c in sorted(images[image_index].keys())]
                per_image_results.append(merge_chunk_results(chunk_results))

            record = load_article_record(article_dir)
            record = finalize_record(record, per_image_results)
            destination = await route_and_save(record, article_dir, cases_col, queue_col)
            counts[destination] += 1
            print(f"{article_id} -> {destination}")
        except Exception as e:
            failed.append(article_id)
            print(f"{article_id} -> [ERR] {e}")

    print("=" * 50)
    print(f"estimate_cases: {counts['estimate_cases']}건")
    print(f"review_queue:   {counts['review_queue']}건")
    if failed:
        print(f"실패: {len(failed)}건 -> {failed}")
    print("=" * 50)

    _state_path(base_dir).unlink(missing_ok=True)
    client.close()


async def cmd_realtime(base_dir: str, limit: int | None, force: bool, dry_run: bool) -> None:
    client, cases_col, queue_col, dirs = await _select_new_dirs(base_dir, limit, force)
    if dry_run:
        for d in dirs[:10]:
            print(f"  - {d}")
        if len(dirs) > 10:
            print(f"  ... 외 {len(dirs) - 10}개")
        print("[DRY-RUN] 실제 파싱/저장은 하지 않음")
        client.close()
        return

    counts = {"estimate_cases": 0, "review_queue": 0}
    failed = []
    for i, article_dir in enumerate(dirs):
        try:
            destination = await ingest_article(str(article_dir), cases_col, queue_col)
            counts[destination] += 1
            print(f"[{i + 1}/{len(dirs)}] {article_dir.name} -> {destination}")
        except Exception as e:
            failed.append(article_dir.name)
            print(f"[{i + 1}/{len(dirs)}] {article_dir.name} -> [ERR] {e}")

    print("=" * 50)
    print(f"estimate_cases: {counts['estimate_cases']}건")
    print(f"review_queue:   {counts['review_queue']}건")
    if failed:
        print(f"실패: {len(failed)}건 -> {failed}")
    print("=" * 50)
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-dir", type=str, default="./estimate_data")
    sub = parser.add_subparsers(dest="action", required=True)

    p_submit = sub.add_parser("submit", help="배치 제출")
    p_submit.add_argument("--limit", type=int, default=None)
    p_submit.add_argument("--force", action="store_true")
    p_submit.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="배치 진행 상황 확인")
    sub.add_parser("collect", help="완료된 배치 결과 수집·저장")

    p_realtime = sub.add_parser("realtime", help="실시간 파싱(즉시 API 호출, 소량 테스트용)")
    p_realtime.add_argument("--limit", type=int, default=None)
    p_realtime.add_argument("--force", action="store_true")
    p_realtime.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.action == "submit":
        asyncio.run(cmd_submit(args.base_dir, args.limit, args.force, args.dry_run))
    elif args.action == "status":
        cmd_status(args.base_dir)
    elif args.action == "collect":
        asyncio.run(cmd_collect(args.base_dir))
    elif args.action == "realtime":
        asyncio.run(cmd_realtime(args.base_dir, args.limit, args.force, args.dry_run))
