from pipeline.crawl_parsing import (
    collect_image_urls_from_html,
    extract_article_id,
    extract_pcarpenter_links,
    is_valid_estimate_image,
    parse_article_rows,
    parse_estimate_detail,
    parse_estimate_link,
    parse_request_body,
    upgrade_image_url,
)


def test_is_valid_estimate_image_filters_noise():
    assert is_valid_estimate_image("https://x.com/postfiles.pstatic.net/abc.jpg") is True
    assert is_valid_estimate_image("https://x.com/logo_icon_123.png") is False
    assert is_valid_estimate_image("https://x.com/필독_큰_버튼.png") is False


def test_upgrade_image_url_forces_w1600():
    url = 'https://postfiles.pstatic.net/abc.jpg?type=w773'
    assert upgrade_image_url(url) == "https://postfiles.pstatic.net/abc.jpg?type=w1600"


def test_extract_article_id_from_url():
    url = "https://cafe.naver.com/f-e/cafes/17593353/articles/850833?boardtype=L&articleid=850833"
    assert extract_article_id(url) == "850833"
    assert extract_article_id("https://no-id-here.com") is None


def test_parse_article_rows():
    html = """
    <table class="article-board"><tbody>
      <tr><td><a class="article" href="/f-e/cafes/1/articles/111">첫번째 글</a></td></tr>
      <tr><td><a class="article" href="/f-e/cafes/1/articles/222">두번째 글</a></td></tr>
      <tr><td>공지 행 (링크 없음)</td></tr>
    </tbody></table>
    """
    rows = parse_article_rows(html)
    assert len(rows) == 2
    assert rows[0]["title"] == "첫번째 글"
    assert rows[0]["url"] == "https://cafe.naver.com/f-e/cafes/1/articles/111"


def test_collect_image_urls_from_html_filters_and_dedups():
    html = """
    <div>
      <img src="https://postfiles.pstatic.net/a.jpg?type=w773">
      <img src="https://postfiles.pstatic.net/a.jpg?type=w773">
      <img src="https://cafeptthumb-phinf.pstatic.net/b.jpg?type=w80">
      <img src="https://example.com/logo_icon.png">
      <img src="https://example.com/unrelated.png">
    </div>
    """
    urls = collect_image_urls_from_html(html)
    assert urls == [
        "https://postfiles.pstatic.net/a.jpg?type=w1600",
        "https://cafeptthumb-phinf.pstatic.net/b.jpg?type=w1600",
    ]


def test_parse_estimate_link_finds_pcarpenter_href():
    html = '<div><a href="https://other.com">x</a><a href="https://cafe.naver.com/pcarpenter/999">견적의뢰</a></div>'
    assert parse_estimate_link(html) == "https://cafe.naver.com/pcarpenter/999"


def test_parse_estimate_link_returns_none_when_missing():
    assert parse_estimate_link("<div><a href='https://other.com'>x</a></div>") is None


def test_parse_estimate_detail_extracts_fields():
    html = """
    <div class="se-main-container">
      공사지역 : 서울시 강남구
      공사희망일 : 2026-01-15
      지정 열린업체명 : 박목수
      25평 아파트 전체 리모델링 원합니다
      <img src="https://postfiles.pstatic.net/a.jpg?type=w773">
      https://cafe.naver.com/pcarpenter/12345 참고하세요
    </div>
    """
    record = parse_estimate_detail(html)
    assert record is not None
    assert record["location"] == "서울시 강남구"
    assert record["deadline"] == "2026-01-15"
    assert record["company"] == "박목수"
    assert record["size_pyeong"] == 25
    assert record["request_url"] == "https://cafe.naver.com/pcarpenter/12345"
    assert record["image_urls"] == ["https://postfiles.pstatic.net/a.jpg?type=w1600"]


def test_parse_estimate_detail_returns_none_on_empty_body():
    assert parse_estimate_detail("<div>본문 없음</div>") is None


def test_parse_request_body_extracts_text():
    html = '<div id="postContent">고객 요청 원문입니다</div>'
    assert parse_request_body(html) == "고객 요청 원문입니다"


def test_extract_pcarpenter_links_finds_all():
    text = "링크1 https://cafe.naver.com/pcarpenter/111 링크2 https://cafe.naver.com/pcarpenter/222"
    assert extract_pcarpenter_links(text) == [
        "https://cafe.naver.com/pcarpenter/111",
        "https://cafe.naver.com/pcarpenter/222",
    ]


def test_extract_pcarpenter_links_empty_when_none():
    assert extract_pcarpenter_links("링크 없음") == []
