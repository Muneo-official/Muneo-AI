from pipeline.crawl_region import detect_region


def test_detect_region_from_location_field():
    assert detect_region({"location": "서울시 강남구"}) == "서울"
    assert detect_region({"location": "경기도 성남시"}) == "경기"


def test_detect_region_prefers_gyeonggi_gwangju_over_gwangju_metro():
    assert detect_region({"location": "경기도 광주시"}) == "경기"


def test_detect_region_recognizes_informal_gwangju_metro_via_gu_name():
    # "광주광역시" 정식 명칭 없이 "광주시 광산구"처럼 구어체로 쓰는 게 실제로 더 흔하다
    # (실측: 885880 사례) — 광산구는 광주광역시에만 있는 구라 경기도 광주시와 안 겹친다.
    assert detect_region({"location": "광주시 광산구 운남동 운남우방아이유쉘"}) == "광주"


def test_detect_region_falls_back_to_other_text():
    data = {"location": "", "post_title": "부산 인테리어 견적 부탁드려요"}
    assert detect_region(data) == "부산"


def test_detect_region_strips_company_name_pattern():
    data = {"location": "", "post_title": "서울24호 업체 견적서, 실제 공사지역은 대전임"}
    assert detect_region(data) == "대전"


def test_detect_region_returns_gita_when_unknown():
    assert detect_region({"location": "", "post_title": "정보 없음"}) == "기타"
