from scripts.audit_content_mismatch import distinctive_tokens, is_generic_region_token, is_mismatch, strip_boilerplate


def test_is_generic_region_token_flags_broad_admin_units():
    assert is_generic_region_token("인천시") is True
    assert is_generic_region_token("중구") is True
    assert is_generic_region_token("경기도") is True
    assert is_generic_region_token("중산동") is False
    assert is_generic_region_token("우미리2차") is False


def test_distinctive_tokens_strips_tags_and_stopwords():
    title = "[**19번님 견적서]인천시 중구 중산동 우미리2차 아파트 리모델링 열린견적서"
    tokens = distinctive_tokens(title)
    assert "중산동" in tokens
    assert "우미리2차" in tokens
    assert "아파트" not in tokens
    assert "열린견적서" not in tokens


def test_is_mismatch_true_when_only_generic_region_overlaps():
    # 872508/857571류 실측 사례 — "인천시"만 겹치고 진짜 구체 지명은 안 겹침
    title = "[**19번님 견적서]인천시 중구 중산동 우미리2차 아파트 리모델링 열린견적서"
    body_text = "■ 제목 :인천시 미추홀구 용현동 신창미션힐 아파트 리모델링 열린견적서"
    mismatch, tokens = is_mismatch(title, body_text, "")
    assert mismatch is True
    assert "중산동" in tokens


def test_is_mismatch_false_when_specific_token_matches():
    title = "[**19번님 견적서]인천시 중구 중산동 우미리2차 아파트 리모델링 열린견적서"
    body_text = "인천시 중구 중산동 우미리2차 아파트 견적을 올려드립니다"
    mismatch, tokens = is_mismatch(title, body_text, "")
    assert mismatch is False


def test_is_mismatch_false_when_no_distinctive_tokens_to_judge():
    title = "[열린견적서 의뢰] 견적 부탁드립니다"
    mismatch, tokens = is_mismatch(title, "아무 내용", "")
    assert mismatch is False
    assert tokens == []


def test_is_mismatch_false_when_body_is_pure_company_signature():
    # 848476/849496 실측 사례 — 이미지만 있고 본문은 회사 광고 문구뿐, 비교할 내용이 없음
    title = "[**40번 고객님의 견적서] 경기도 화성시 기산동 행림마을 래미안 33평형 아파트"
    body_text = (
        "★ [성공적인 인테리어 공사를 위한 안내서] 바로가기\n"
        "★ [경기14호 시공갤러리] 바로가기\n★ [경기14호 견적방] 바로가기"
    )
    mismatch, tokens = is_mismatch(title, body_text, "")
    assert mismatch is False


def test_is_mismatch_true_when_real_content_mismatches_despite_signature_lines():
    # 서명 문구가 섞여 있어도 진짜 불일치 내용은 걸러지면 안 된다
    title = "[**19번님 견적서]인천시 중구 중산동 우미리2차 아파트 리모델링 열린견적서"
    body_text = (
        "■ 제목 :  인천시 미추홀구 용현동 신창미션힐 아파트 리모델링 열린견적서\n"
        "친절한 상담을 원하시는 고객님께서는 링크 주소로 신청해 주세요~"
    )
    mismatch, tokens = is_mismatch(title, body_text, "")
    assert mismatch is True


def test_strip_boilerplate_removes_signature_lines_only():
    text = "인천시 미추홀구 용현동 신창미션힐\n★ [경기14호 시공갤러리] 바로가기"
    stripped = strip_boilerplate(text)
    assert "신창미션힐" in stripped
    assert "시공갤러리" not in stripped
