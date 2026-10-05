"""
eval/quote_ground_truth.py의 derive()·check_record() 단위테스트 — DB 없이 도는 순수 계산 부분만.

정답셋의 공종별 금액·비교 총액·입력 공종은 사람이 적지 않고 derive()가 견적서 값(quote 블록)에서 계산한다.
여기가 틀리면 정답셋 전부가 같은 방향으로 틀리므로 계산 규칙을 테스트로 고정해 둔다. 금액은 지어낸 값이다.
"""

import copy

from eval.quote_ground_truth import HOLDOUT_QUOTA, _scope, check_record, derive, holdout_targets

_DOOR = {"창호공사": {"도어": 3_000_000}, "목공사": {"도어": 0}}


def _record() -> dict:
    """확장공사(매핑 불가)가 있고 욕실·전기가 두 섹션으로 나뉜 전체 시공 견적서 한 장."""
    sections = [
        ("확장공사", 6_000_000, "매핑 불가"), ("창호공사", 15_000_000, "창호"), ("타일공사", 4_000_000, "욕실"),
        ("욕실공사", 3_000_000, "욕실"), ("가구공사", 2_000_000, "가구"), ("전기공사", 1_000_000, "전기/조명"),
        ("조명공사", 1_500_000, "전기/조명"), ("시트공사", 1_000_000, "필름"), ("바닥공사", 2_000_000, "장판"),
        ("도배공사", 3_500_000, "도배"), ("목공사", 1_500_000, "목공"), ("철거공사", 3_000_000, "철거"),
        ("기타공사", 1_500_000, "마감/공과잡비"),
    ]
    record = {
        "id": "gt-test", "status": "verified", "split": "dev",
        "quote": {
            # 창호공사 15,000,000 중 3,000,000이 도어, 목공사에는 도어가 없다
            "sections": [{"name": n, "amount": a, "공종": g, **_DOOR.get(n, {})} for n, a, g in sections],
            "직접비_합계": 45_000_000,
            "간접비_내역": {"이윤": 3_150_000, "보험료": 684_000, "단수": -4_000},
            "총액_부가세제외": 48_830_000,
            "부가세_표기": "포함",
        },
        "input": {
            "공종": [], "시공범위": "부분", "공간유형": "아파트", "평수": 32, "방개수": 3, "지역": "서울",
            "건물연식": "10~20년", "자재등급": "중급", "철거여부": "없음", "층수": 1, "엘리베이터": "있음",
            "트럭접근": "가능", "거주중공사": "공실", "공사시기": "미정",
            "도배": {"범위": "전체", "도배지종류": "실크벽지"}, "마루": {"범위": "전체"}, "욕실": {"개수": 2},
        },
        "truth": {}, "flags": [], "notes": "",
    }
    derive(record)
    return record


def test_같은_공종의_섹션은_합산하고_매핑_불가는_따로_둔다():
    truth = _record()["truth"]

    assert truth["공종별"]["욕실"] == 4_000_000 + 3_000_000
    assert truth["공종별"]["전기/조명"] == 1_000_000 + 1_500_000
    assert truth["매핑_불가"] == [{"항목": "확장공사", "금액": 6_000_000}]
    assert sum(truth["공종별"].values()) + 6_000_000 == truth["직접비_합계"]


def test_비교_총액은_매핑_불가에_붙은_간접비까지_비례해서_뺀다():
    truth = _record()["truth"]

    # 48,830,000 − 6,000,000 × (48,830,000 ÷ 45,000,000) = 42,319,333.3…
    assert truth["비교_총액"] == 42_319_333
    assert truth["비교_직접비"] == 45_000_000 - 6_000_000
    assert truth["간접비"] == 48_830_000 - 45_000_000


def test_매핑_불가가_직접비의_10퍼센트를_넘으면_플래그를_붙인다():
    record = _record()

    assert record["flags"] == ["매핑불가_10%초과"]  # 6,000,000 ÷ 45,000,000 = 13.3%

    record["quote"]["sections"][0]["amount"] = 4_000_000  # 9.3%로 낮추면 플래그가 사라져야 한다
    record["quote"]["직접비_합계"] -= 2_000_000
    derive(record)

    assert record["flags"] == []


def test_입력의_공종_시공범위_철거여부_옵션은_정답_공종에서_정한다():
    inp = _record()["input"]

    assert inp["공종"] == ["창호", "도어", "욕실", "가구", "전기/조명", "필름", "장판", "도배", "목공", "철거", "마감/공과잡비"]
    assert inp["시공범위"] == "전체"
    assert inp["철거여부"] == "있음"
    assert "마루" not in inp  # 바닥이 장판이므로 마루 옵션은 지운다
    assert inp["욕실"] == {"개수": 2}


def test_시공범위는_같은_공종을_한_번만_센다():
    # 초안에서는 타일·도기·수전 섹션이 각각 '욕실'로 들어온다. 중복을 세면 공종 4개짜리가 전체로 잘못 나온다.
    assert _scope(["욕실", "욕실", "욕실", "도배", "장판", "창호", "매핑 불가", "마감/공과잡비"]) == "부분"
    assert _scope(["욕실", "도배", "장판", "창호", "가구", "목공"]) == "전체"
    assert _scope(["욕실", "도배", "창호", "가구", "목공", "철거"]) == "부분"  # 바닥이 없으면 부분
    # 도어는 창호와 한 공종으로 센다. 따로 세면 공종 5개짜리가 6개가 돼 전체로 바뀐다
    assert _scope(["욕실", "도배", "장판", "창호", "도어", "가구"]) == "부분"
    assert _scope(["욕실", "도배", "장판", "도어", "가구", "목공"]) == "전체"


def test_검산을_통과한_레코드는_문제가_없다():
    assert check_record(_record()) == []


def test_섹션_합이_공사비와_다르면_잡아낸다():
    record = _record()
    record["quote"]["sections"][1]["amount"] += 10_000
    derive(record)

    assert any("섹션 합" in p for p in check_record(record))


def test_공종_오타는_조용히_빠지지_않고_잡힌다():
    record = _record()
    record["quote"]["sections"][4]["공종"] = "가구공사"  # '가구'의 오타
    derive(record)

    problems = check_record(record)

    assert any("알 수 없는 공종" in p for p in problems)


def test_quote를_고치고_build를_안_돌리면_잡아낸다():
    record = _record()
    stale = copy.deepcopy(record)
    stale["quote"]["sections"][8]["공종"] = "마루"  # truth·input은 장판인 채로 둔다

    assert any("build를 다시 실행" in p for p in check_record(stale))


def test_검수_완료인데_초안_흔적이_남아_있으면_잡아낸다():
    record = _record()
    record["flags"].append("품목합_총액초과")
    record["quote"]["부가세_표기"] = "불명"
    derive(record)

    problems = check_record(record)

    assert any("미확인" in p for p in problems)
    assert any("초안 전용 플래그" in p for p in problems)


# ── 도어 분리 (규칙 1.1) ──────────────────────────────────────────────────


def test_섹션의_도어_금액은_도어_공종으로_떼어_낸다():
    truth = _record()["truth"]

    assert truth["공종별"]["창호"] == 15_000_000 - 3_000_000
    assert truth["공종별"]["도어"] == 3_000_000
    assert truth["공종별"]["목공"] == 1_500_000
    assert sum(truth["공종별"].values()) + 6_000_000 == truth["직접비_합계"]  # 떼어도 합은 그대로


def test_목공_섹션에_든_도어도_도어_공종으로_모은다():
    record = _record()
    record["quote"]["sections"][10]["도어"] = 500_000  # 목공사 1,500,000 중 도어 500,000
    derive(record)

    assert record["truth"]["공종별"]["목공"] == 1_000_000
    assert record["truth"]["공종별"]["도어"] == 3_000_000 + 500_000


def test_창호_섹션이_도어뿐이면_창호_공종은_없고_플래그도_지운다():
    record = _record()
    record["quote"]["sections"][1]["도어"] = 15_000_000
    record["flags"].append("창호_도어만")  # 규칙 1.0에서 붙였던 플래그
    derive(record)

    assert "창호" not in record["truth"]["공종별"]
    assert "창호" not in record["input"]["공종"] and "도어" in record["input"]["공종"]
    assert "창호_도어만" not in record["flags"]


def test_도어를_확인하지_못한_섹션은_떼지_않고_플래그를_붙인다():
    record = _record()
    record["quote"]["sections"][1]["도어"] = None
    derive(record)

    assert record["truth"]["공종별"]["창호"] == 15_000_000
    assert "도어" not in record["truth"]["공종별"]
    assert "도어_미확인" in record["flags"]
    assert check_record(record) == []  # 확인 못 한 것은 문제가 아니라 플래그로 남긴다

    record["quote"]["sections"][1]["도어"] = 3_000_000  # 확인하면 플래그가 사라진다
    derive(record)
    assert "도어_미확인" not in record["flags"]


def test_창호_목공_섹션에_도어_금액을_안_적으면_잡아낸다():
    record = _record()
    del record["quote"]["sections"][10]["도어"]
    derive(record)

    assert any("도어 금액이 없음" in p for p in check_record(record))


def test_도어_금액이_섹션_금액보다_크면_잡아낸다():
    record = _record()
    record["quote"]["sections"][10]["도어"] = 2_000_000  # 목공사는 1,500,000
    derive(record)

    assert any("벗어남" in p for p in check_record(record))


# ── 검증용(holdout) 세트 ───────────────────────────────────────────────────

칸 = "서울/30평대/전체"


def _cand(article_id: str, role: str = "reserve", cell: str = 칸) -> dict:
    return {"article_id": article_id, "request_url": f"https://example.com/{article_id}", "cell": cell,
            "region": "서울", "size_pyeong": 32, "split": "eval", "role": role}


def _gt(article_id: str, split: str, status: str = "verified", cell: str = 칸) -> dict:
    return {"split": split, "status": status, "source": {"article_id": article_id, "cell": cell}}


def test_검증용_칸별_건수는_평가용과_같고_모두_20건이다():
    assert sum(HOLDOUT_QUOTA.values()) == 20
    assert HOLDOUT_QUOTA[칸] == 4


def test_검증용은_예비_후보를_목록_순서대로_고르고_이미_쓴_건은_건너뛴다():
    cands = [_cand("1", role="selected"), _cand("2"), _cand("3"), _cand("4"), _cand("5"), _cand("6"), _cand("7")]
    records = [_gt("1", "eval"), _gt("2", "eval")]  # 2번은 평가용에서 탈락한 건을 대체하며 이미 썼다

    targets, short = holdout_targets(cands, records)

    assert [c["article_id"] for c in targets] == ["3", "4", "5", "6"]
    assert all(c["split"] == "holdout" for c in targets)
    assert 칸 not in short


def test_검증용에서_탈락한_건은_같은_칸의_다음_후보로_채운다():
    cands = [_cand(str(i)) for i in range(1, 8)]
    records = [_gt("1", "holdout"), _gt("2", "holdout", status="excluded"), _gt("3", "holdout"), _gt("4", "holdout")]

    targets, _ = holdout_targets(cands, records)

    assert [c["article_id"] for c in targets] == ["5"]


def test_검증용_예비_후보가_모자란_칸을_알린다():
    targets, short = holdout_targets([_cand("1"), _cand("2")], [])

    assert [c["article_id"] for c in targets] == ["1", "2"]
    assert short[칸] == 2
