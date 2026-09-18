"""박목수 열린견적서 카페 크롤러의 순수 파싱 로직 — 브라우저 자동화(Selenium)와 분리.

pipeline/reference/crawler.py는 "페이지 이동(Selenium)"과 "받아온 HTML에서 데이터
추출(BeautifulSoup)"이 함수 하나에 섞여 있어서, 브라우저 없이는 파싱 로직 자체도
테스트할 수 없었다. 이 모듈은 후자만 떼어냈다 — 입력은 전부 이미 받아온 HTML 문자열
이라 고정 HTML 픽스처로 실제 단위테스트가 가능하다.

브라우저 자동화(페이지 이동, 로그인, iframe 진입)는 pipeline/crawler.py에 남아있고,
그 모듈이 이 모듈의 함수들을 호출해 실제 파싱을 수행한다.
"""

import re

from bs4 import BeautifulSoup

# ── 본문 컨테이너 선택 (여러 함수가 공유) ──────────────────


def _select_body(soup: BeautifulSoup):
    """게시글 본문 컨테이너를 찾는다.

    페이지 전체(soup)에서 곧장 찾지 않고 반드시 이 컨테이너로 범위를 좁혀야 한다 —
    사이드바의 "최근 게시글"/추천 위젯도 같은 페이지 안에 이미지·pcarpenter 링크를
    가지고 있어서, 스코프 없이 찾으면 이 글과 무관한 다른 고객의 콘텐츠를 집어올 수
    있다(실측: parse_estimate_link가 본문과 전혀 다른 주소의 견적서로 연결되는 현상으로
    확인됨).
    """
    return (
        soup.select_one(".se-main-container")
        or soup.select_one(".ContentRenderer")
        or soup.select_one("#postContent")
    )


# ── 이미지 필터링 / 화질 업그레이드 ──────────────────────

ESTIMATE_IMAGE_DOMAINS = (
    "postfiles.pstatic.net",  # 네이버 카페 첨부 이미지(사용자가 직접 올린 사진)
    "cafeptthumb",  # 네이버 카페 썸네일
    "parkcarpenter.com",  # 박목수 "열린견적서" 위젯이 자체 서버에서 생성하는 견적표 이미지
)

NOISE_KEYWORDS = [
    "logo_icon",
    "필독_큰_버튼",
    "dthumb-phinf",
    "f100_100",
    "f1480_240_banner",
    "ConfigProfileFileName",   # 업체 프로필 사진
    "로고",                    # 업체 로고
]


def is_valid_estimate_image(url: str) -> bool:
    return not any(kw in url for kw in NOISE_KEYWORDS)


def upgrade_image_url(url: str) -> str:
    """썸네일 URL → 원본 크기로 변환 (type=w1600)."""
    return re.sub(r'type=[^&"\']+', "type=w1600", url)


def extract_pdf_attachment_url(html: str) -> str | None:
    """게시글 본문의 "파일 다운로드" 위젯(PDF 첨부 열린견적서)에서 다운로드 링크를 찾는다.

    이런 게시글은 <img> 태그가 아예 없이 견적서를 PDF 첨부파일로만 올린다 — 스마트에디터가
    첨부파일을 `<a class="se-file-save-button" href="https://downapi.cafe.naver.com/...">`로
    렌더링하는데, 이 href가 곧 다운로드 URL이다.
    """
    soup = BeautifulSoup(html, "html.parser")
    body_el = _select_body(soup)
    if body_el is None:
        return None
    link = body_el.select_one("a.se-file-save-button")
    return link.get("href") or None if link else None


# ── 게시글 목록 페이지 ────────────────────────────────────


def parse_article_rows(html: str) -> list[dict]:
    """게시글 목록 페이지 HTML에서 [{"title", "url"}, ...] 추출."""
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select(".article-board tbody tr")

    articles = []
    for row in rows:
        title_el = row.select_one("a.article")
        if not title_el:
            continue
        href = title_el.get("href", "")
        full_url = "https://cafe.naver.com" + href if href.startswith("/") else href
        articles.append({"title": title_el.get_text(strip=True), "url": full_url})
    return articles


def extract_article_id(url: str) -> str | None:
    """URL에서 게시글 번호를 뽑는다.

    두 가지 URL 형태를 다 지원해야 한다 — `.../ArticleRead.nhn?...articleid=12345`
    (긴 iframe_url 형태)와 `https://cafe.naver.com/pcarpenter/12345`(짧은 리다이렉트
    형태). 업체마다 게시글에 심어두는 링크 형태가 달라서(예: 경북3호는 짧은 형태만 씀),
    긴 형태만 지원하면 매칭 실패 시 호출자가 반복문 인덱스(0, 1, 2...)로 폴백하게 되고,
    그러면 서로 다른 크롤링 실행끼리 article_id가 충돌해 데이터를 덮어쓸 수 있다.
    """
    m = re.search(r"articleid(?:%3D|=)(\d+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"pcarpenter/(\d+)(?:[/?]|$)", url)
    return m.group(1) if m else None


# ── 이미지 URL 수집 (여러 화면에서 공통으로 쓰는 로직) ──────


def collect_image_urls_from_html(html: str) -> list[str]:
    """본문 컨테이너 안의 <img> 태그에서 견적서 이미지로 보이는 URL만 골라 원본 크기로
    업그레이드한다. 중복 제거."""
    soup = BeautifulSoup(html, "html.parser")
    body_el = _select_body(soup)
    if body_el is None:
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for img in body_el.select("img"):
        src = img.get("src") or img.get("data-lazy-src") or img.get("data-src") or ""
        if not src or not any(domain in src for domain in ESTIMATE_IMAGE_DOMAINS):
            continue
        if not is_valid_estimate_image(src):
            continue
        upgraded = upgrade_image_url(src)
        if upgraded not in seen:
            seen.add(upgraded)
            urls.append(upgraded)
    return urls


# ── 견적방 게시글 → 견적의뢰 링크 ─────────────────────────


def parse_estimate_link(html: str) -> str | None:
    """게시글 본문 HTML에서 pcarpenter 견적의뢰 링크를 추출한다.

    반드시 본문 컨테이너(_extract_body_text와 동일한 선택자) 안에서만 찾는다 — 페이지
    전체에서 찾으면 사이드바의 "최근 게시글"/추천 위젯에 있는, 이 글과 무관한 다른
    고객의 pcarpenter 링크를 잘못 집어올 수 있다(실측: 특정 게시글에서 본문과 전혀
    다른 주소·평수의 견적서가 연결되는 현상으로 확인됨).
    """
    soup = BeautifulSoup(html, "html.parser")
    body_el = _select_body(soup)
    if body_el is None:
        return None
    for a in body_el.select("a"):
        href = a.get("href", "")
        if "cafe.naver.com/pcarpenter" in href:
            return href
    return None


# ── 견적의뢰 원문 ─────────────────────────────────────────


def _extract_body_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    body_el = _select_body(soup)
    return body_el.get_text("\n", strip=True) if body_el else ""


def extract_size_pyeong(text: str) -> int:
    """텍스트에서 평수를 뽑는다. "평" 대신 "py"(예: "32py")로 표기하는 업체 글이 많아서
    (실측: size_pyeong=0으로 빠진 855건 중 168건이 이 표기 때문이었음) 둘 다 매칭한다.
    \\b로 "pyramid" 같은 단어 일부는 제외. 못 찾으면 0."""
    m = re.search(r"(\d+)\s*(?:평|py\b)", text, re.IGNORECASE)
    return int(m.group(1)) if m else 0


def parse_estimate_detail(html: str) -> dict | None:
    """견적의뢰 원문 페이지 HTML에서 공사정보 + 이미지 URL을 추출한다.

    본문이 비어있으면(iframe 진입은 됐지만 내용 로딩 실패 등) None — 호출자가 재시도
    여부를 판단한다.
    """
    body_text = _extract_body_text(html)
    if not body_text:
        return None

    def extract(pattern: str) -> str:
        m = re.search(pattern, body_text)
        return m.group(1).strip() if m else ""

    request_url_match = re.search(r"https?://cafe\.naver\.com/pcarpenter/(\d+)", body_text)

    return {
        "location": extract(r"공사지역\s*[:：]\s*(.+)"),
        "deadline": extract(r"공사희망일\s*[:：]\s*(.+)"),
        "company": extract(r"지정\s*열린업체명\s*[:：]\s*(.+)"),
        "size_pyeong": extract_size_pyeong(body_text),
        "body_text": body_text,
        "request_url": request_url_match.group(0) if request_url_match else "",
        "image_urls": collect_image_urls_from_html(html),
        "pdf_attachment_url": extract_pdf_attachment_url(html),
    }


def parse_request_body(html: str) -> str:
    """견적의뢰글(고객 원문) 페이지 HTML에서 본문 텍스트만 추출한다."""
    return _extract_body_text(html)


def extract_pcarpenter_links(body_text: str) -> list[str]:
    """본문 텍스트 안에 있는 pcarpenter 게시글 링크를 전부 찾는다 (중복 포함, 순서 유지).

    래퍼 포스트(열린견적서 목록)가 실제 견적 이미지를 링크로만 가리킬 때, 그 링크들을
    하나씩 방문해 이미지를 추가로 모으는 데 쓰인다.
    """
    return re.findall(r"https?://cafe\.naver\.com/pcarpenter/\d+", body_text)
