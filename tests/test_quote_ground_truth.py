"""
eval/quote_ground_truth.py의 derive()·check_record() 단위테스트 — DB 없이 도는 순수 계산 부분만.

정답셋의 공종별 금액·비교 총액·입력 공종은 사람이 적지 않고 derive()가 견적서 값(quote 블록)에서 계산한다.
여기가 틀리면 정답셋 전부가 같은 방향으로 틀리므로 계산 규칙을 테스트로 고정해 둔다. 금액은 지어낸 값이다.
"""

import copy

from eval.quote_ground_truth import _scope, check_record, derive


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
            "sections": [{"name": n, "amount": a, "공종": g} for n, a, g in sections],
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

    assert inp["공종"] == ["창호", "욕실", "가구", "전기/조명", "필름", "장판", "도배", "목공", "철거", "마감/공과잡비"]
    assert inp["시공범위"] == "전체"
    assert inp["철거여부"] == "있음"
    assert "마루" not in inp  # 바닥이 장판이므로 마루 옵션은 지운다
    assert inp["욕실"] == {"개수": 2}


def test_시공범위는_같은_공종을_한_번만_센다():
    # 초안에서는 타일·도기·수전 섹션이 각각 '욕실'로 들어온다. 중복을 세면 공종 4개짜리가 전체로 잘못 나온다.
    assert _scope(["욕실", "욕실", "욕실", "도배", "장판", "창호", "매핑 불가", "마감/공과잡비"]) == "부분"
    assert _scope(["욕실", "도배", "장판", "창호", "가구", "목공"]) == "전체"
    assert _scope(["욕실", "도배", "창호", "가구", "목공", "철거"]) == "부분"  # 바닥이 없으면 부분


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
