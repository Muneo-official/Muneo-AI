import json

from scripts.bench.grade_truth import grade_file, grade_run, summarize


def _truth(case, code, amount, truth):
    return {"case": case, "image_index": 0, "code": code, "amount": amount, "truth": truth}


def _item(code, amount, category):
    return {"code": code, "amount": amount, "category": category, "description": "x"}


def test_grade_run_counts_correct_wrong_and_missing():
    truth = [_truth("S1", "1", 100, "설비"), _truth("S1", "2", 200, "철거"), _truth("S1", "3", 300, "도배")]
    items = [_item("1", 100, "설비"), _item("2", 200, "공과잡비")]  # 3번은 행 누락

    g = grade_run("S1", items, truth)

    assert (g["total"], g["correct"], g["missing"]) == (3, 1, 1)
    assert g["wrong"] == ["2 철거→공과잡비"]


def test_grade_run_matches_by_code_and_amount_and_consumes_duplicates():
    # 금액을 잘못 읽으면 못 찾은 것으로 센다, 같은 code·금액 두 행은 하나씩 매칭
    truth = [_truth("S1", "1", 100, "설비"), _truth("S1", "1", 100, "설비"), _truth("S1", "9", 900, "전기")]
    items = [_item(1, 100, "설비"), _item("1", 100, "욕실"), _item("9", 901, "전기")]

    g = grade_run("S1", items, truth)

    assert (g["correct"], g["missing"], g["wrong"]) == (1, 1, ["1 설비→욕실"])


def test_grade_run_tolerates_malformed_amount():
    # 모델이 금액을 문자열로 내도 채점 전체가 멈추지 않는다 (못 찾은 항목으로 센다)
    truth = [_truth("S1", "1", 100, "설비")]
    items = [{"code": "1", "amount": "약 100원", "category": "설비"}]

    assert grade_run("S1", items, truth)["missing"] == 1


def test_grade_file_and_summarize(tmp_path):
    film_items = [_item("1001", 3_500_000, "필름")]
    result = {
        "label": "m", "model": "claude-haiku-4-5",
        "requests": [
            {"case_id": "S1", "warmup": False, "status": 200, "wall_s": 10.0, "cost_usd": 0.01,
             "parsed": {"line_items": [_item("1", 100, "설비")]},
             "signature": {"line_item_count": 28, "amount_sum": 7_386_000, "issues": []},
             "server": {"vision_calls": [{"tool_called": True}]}},
            {"case_id": "S3", "warmup": False, "status": 200, "wall_s": 30.0, "cost_usd": 0.05,
             "parsed": {"line_items": film_items},
             "signature": {"issues": [["필름", "주의", "필름 가격 이상"]]},
             "server": {"vision_calls": [{"tool_called": False, "retried": True}, {"tool_called": True, "retried": True}]}},
            {"case_id": "S1", "warmup": True, "status": 200, "wall_s": 1.0, "parsed": {"line_items": []}},
        ],
    }
    path = tmp_path / "r.json"
    path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    truth = [_truth("S1", "1", 100, "설비"), _truth("S3", "1001", 3_500_000, "필름")]

    s = summarize(grade_file(path, truth))

    assert s["truth_accuracy"] == 1.0
    assert (s["s1_exact"], s["film_ok"], s["film_issue"]) == ("1/1", "1/1", "1/1")
    assert s["tool_not_called"] == 1  # 재시도 후에도 실패한 호출
    assert s["retried"] == 2  # 재시도가 성공한 호출도 따로 센다
    assert s["cases"]["S1"]["n"] == 1  # 워밍업 제외
