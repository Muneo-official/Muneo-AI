import json
import pathlib

import pytest

import pipeline.ingest as ingest_module
from pipeline.ingest import ingest_article, load_article_record, process_article


def _write_article(tmp_path: pathlib.Path, article_id: str, **fields) -> str:
    article_dir = tmp_path / article_id
    article_dir.mkdir()
    record = {"article_id": article_id, "region": "서울", "size_pyeong": 33, **fields}
    (article_dir / f"{article_id}.json").write_text(
        json.dumps(record, ensure_ascii=False), encoding="utf-8"
    )
    return str(article_dir)


def test_load_article_record_reads_json(tmp_path: pathlib.Path):
    article_dir = _write_article(tmp_path, "123", request_body_text="테스트")
    record = load_article_record(article_dir)
    assert record["article_id"] == "123"
    assert record["request_body_text"] == "테스트"


def test_process_article_fills_derived_fields(tmp_path: pathlib.Path, monkeypatch):
    article_dir = _write_article(
        tmp_path, "123", local_images=["a.png"], request_body_text="도배 리모델링 요청"
    )
    monkeypatch.setattr(
        ingest_module,
        "parse_image",
        lambda path: {
            "is_estimate": True,
            "total_cost": 1_000_000,
            "line_items": [{"category": "도배", "description": "실크벽지", "amount": 1_000_000}],
        },
    )

    record = process_article(article_dir)

    assert record["total_cost"] == 1_000_000
    assert record["cost_per_pyeong"] == 1_000_000 // 33
    assert record["cost_도배"] == 1_000_000
    assert record["has_도배"] == "true"
    assert record["has_욕실"] == "false"
    assert record["_validation"]["confidence"] == 1.0


def test_process_article_no_estimate_images_returns_empty_parsed_estimate(
    tmp_path: pathlib.Path, monkeypatch
):
    article_dir = _write_article(tmp_path, "456", local_images=["logo.png"])
    monkeypatch.setattr(ingest_module, "parse_image", lambda path: {"is_estimate": False})

    record = process_article(article_dir)

    assert record["parsed_estimate"] == {}
    assert record["_validation"] is None


class _FakeCollection:
    def __init__(self):
        self.saved: list[tuple[dict, dict]] = []

    async def update_one(self, filter_, update, upsert=False):
        self.saved.append((filter_, update["$set"]))


@pytest.mark.asyncio
async def test_ingest_article_routes_high_confidence_to_estimate_cases(
    tmp_path: pathlib.Path, monkeypatch
):
    article_dir = _write_article(tmp_path, "789", local_images=["a.png"])
    monkeypatch.setattr(
        ingest_module,
        "parse_image",
        lambda path: {
            "is_estimate": True,
            "total_cost": 500_000,
            "line_items": [{"category": "도장", "description": "페인트", "amount": 500_000}],
        },
    )
    cases_col, queue_col = _FakeCollection(), _FakeCollection()

    destination = await ingest_article(article_dir, cases_col, queue_col)

    assert destination == "estimate_cases"
    assert len(cases_col.saved) == 1
    assert len(queue_col.saved) == 0


@pytest.mark.asyncio
async def test_ingest_article_routes_no_estimate_to_review_queue(
    tmp_path: pathlib.Path, monkeypatch
):
    article_dir = _write_article(tmp_path, "999", local_images=["logo.png"])
    monkeypatch.setattr(ingest_module, "parse_image", lambda path: {"is_estimate": False})
    cases_col, queue_col = _FakeCollection(), _FakeCollection()

    destination = await ingest_article(article_dir, cases_col, queue_col)

    assert destination == "review_queue"
    assert len(queue_col.saved) == 1
    assert len(cases_col.saved) == 0


@pytest.mark.asyncio
async def test_ingest_article_routes_low_confidence_to_review_queue(
    tmp_path: pathlib.Path, monkeypatch
):
    # size_pyeong 오염(범위 밖, error) + 미인식 category가 total_cost 전액을 차지(error)
    # 두 error가 겹쳐야 confidence(1.0 - 0.3*2 = 0.4)가 임계값(0.7) 밑으로 떨어진다 —
    # error 하나만으로는 1.0-0.3=0.7이라 경계값 그대로 estimate_cases로 감(의도된 동작,
    # pipeline/routing.py의 "확실히 기준 미달"인 것만 review_queue로 보낸다는 설계).
    article_dir = _write_article(tmp_path, "111", size_pyeong=877930, local_images=["a.png"])
    monkeypatch.setattr(
        ingest_module,
        "parse_image",
        lambda path: {
            "is_estimate": True,
            "total_cost": 500_000,
            "line_items": [{"category": "냉난방공사", "description": "에어컨", "amount": 500_000}],
        },
    )
    cases_col, queue_col = _FakeCollection(), _FakeCollection()

    destination = await ingest_article(article_dir, cases_col, queue_col)

    assert destination == "review_queue"
