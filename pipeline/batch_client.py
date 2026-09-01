"""Anthropic Batches API로 여러 이미지를 한 번에 제출해 파싱 비용을 절감한다
(공식 배치 할인, 실시간 대비 약 50%).

pipeline/reference/parse_estimates.py의 배치 모드를 이관하되, 원본 크롤러의 특정 폴더
구조(estimate_data/{지역}/{article_id}/)에 종속되지 않도록 일반화했다 — 호출자가
(article_id, image_paths) 목록만 주면 된다. 상태 저장(어느 배치를 제출했는지 등)은
호출자 책임으로 남긴다 — 이 모듈은 순수하게 요청 생성/제출/조회/수집 함수만 제공한다.

Anthropic API를 실제로 호출하는 모듈이라 API 키 없이는 단위테스트할 수 없다.
request 생성(build_batch_requests)만 로컬 이미지 파일로 순수 테스트 가능하다.
"""

from collections import defaultdict
from dataclasses import dataclass

import anthropic

from pipeline.crawl_filter import is_boilerplate
from pipeline.image_prep import prepare_chunks
from pipeline.vision_client import build_api_params

BATCH_CHUNK_SIZE = 150  # 배치 하나당 최대 요청 수 (Anthropic 요청 크기 제한 대응)


@dataclass(frozen=True)
class RequestMeta:
    article_id: str
    image_index: int
    chunk_index: int


def build_batch_requests(
    articles: list[tuple[str, list[str]]],
) -> tuple[list[dict], dict[str, RequestMeta]]:
    """articles: [(article_id, [image_path, ...]), ...] -> (요청 목록, custom_id -> meta).

    custom_id 형식: "{article_id}__{image_index}__{chunk_index}" — 결과 수집 시
    이 셋으로 원래 위치를 복원한다.

    알려진 보일러플레이트(pipeline/crawl_filter.py)는 아예 요청 목록에 안 넣는다 —
    배치 요청 자체를 줄여야 실제 비용 절감이 되므로, tool use 응답에서 걸러내는 것보다
    여기서 미리 빼는 게 맞다.
    """
    requests: list[dict] = []
    meta: dict[str, RequestMeta] = {}

    for article_id, image_paths in articles:
        for img_idx, path in enumerate(image_paths):
            if is_boilerplate(path):
                continue
            for chunk_idx, chunk_bytes in enumerate(prepare_chunks(path)):
                custom_id = f"{article_id}__{img_idx}__{chunk_idx}"
                requests.append({"custom_id": custom_id, "params": build_api_params(chunk_bytes)})
                meta[custom_id] = RequestMeta(article_id, img_idx, chunk_idx)

    return requests, meta


def submit_batches(client: anthropic.Anthropic, requests: list[dict]) -> list[dict]:
    """BATCH_CHUNK_SIZE 단위로 나눠 제출.

    반환값을 호출자가 저장해뒀다가 check_batches_status()/collect_batch_results()에 그대로
    넘겨야 한다 — 이 모듈은 상태를 들고 있지 않는다.
    """
    batches = []
    for i in range(0, len(requests), BATCH_CHUNK_SIZE):
        chunk = requests[i : i + BATCH_CHUNK_SIZE]
        batch = client.messages.batches.create(requests=chunk)
        batches.append({"batch_id": batch.id, "custom_ids": [r["custom_id"] for r in chunk]})
    return batches


def check_batches_status(client: anthropic.Anthropic, batches: list[dict]) -> dict:
    """제출된 배치들의 진행 상황 요약."""
    total_ok = total_err = total_proc = 0
    statuses = []
    for b in batches:
        batch = client.messages.batches.retrieve(b["batch_id"])
        counts = batch.request_counts
        total_ok += counts.succeeded
        total_err += counts.errored
        total_proc += counts.processing
        statuses.append({
            "batch_id": b["batch_id"],
            "status": batch.processing_status,
            "succeeded": counts.succeeded,
            "errored": counts.errored,
            "processing": counts.processing,
        })
    return {
        "batches": statuses,
        "succeeded": total_ok,
        "errored": total_err,
        "processing": total_proc,
        "all_ended": all(s["status"] == "ended" for s in statuses),
    }


def collect_batch_results(
    client: anthropic.Anthropic, batches: list[dict], meta: dict[str, RequestMeta]
) -> dict[str, dict[int, dict[int, dict]]]:
    """article_id -> image_index -> chunk_index -> tool_use input(dict).

    all_ended가 True일 때만 호출할 것 — 진행 중인 배치는 결과가 비어있을 수 있다.
    이 결과를 pipeline.parsing.merge_chunk_results()(같은 이미지의 청크들)로 합친 뒤,
    이미지별 결과를 다시 merge_parsed_results()(같은 사례의 여러 페이지)로 합치면 된다.
    """
    out: dict[str, dict[int, dict[int, dict]]] = defaultdict(lambda: defaultdict(dict))
    for b in batches:
        for result in client.messages.batches.results(b["batch_id"]):
            m = meta.get(result.custom_id)
            if m is None or result.result.type != "succeeded":
                continue
            for block in result.result.message.content:
                if block.type == "tool_use":
                    out[m.article_id][m.image_index][m.chunk_index] = block.input
                    break
    return out
