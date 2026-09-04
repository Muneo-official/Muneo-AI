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

# ── 이미지 필터링 / 화질 업그레이드 ──────────────────────

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
    m = re.search(r"articleid(?:%3D|=)(\d+)", url, re.IGNORECASE)
    return m.group(1) if m else None


# ── 이미지 URL 수집 (여러 화면에서 공통으로 쓰는 로직) ──────


def collect_image_urls_from_html(html: str) -> list[str]:
    """페이지 HTML의 <img> 태그에서 견적서 이미지로 보이는 URL만 골라 원본 크기로
    업그레이드한다. 중복 제거."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for img in soup.select("img"):
        src = img.get("src") or img.get("data-lazy-src") or img.get("data-src") or ""
        if not src or ("postfiles.pstatic.net" not in src and "cafeptthumb" not in src):
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
    """게시글 본문 HTML에서 pcarpenter 견적의뢰 링크를 추출한다."""
    soup = BeautifulSoup(html, "html.parser")
    for a in soup.select("a"):
        href = a.get("href", "")
        if "cafe.naver.com/pcarpenter" in href:
            return href
    return None


# ── 견적의뢰 원문 ─────────────────────────────────────────


def _extract_body_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    body_el = (
        soup.select_one(".se-main-container")
        or soup.select_one(".ContentRenderer")
        or soup.select_one("#postContent")
    )
    return body_el.get_text("\n", strip=True) if body_el else ""


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

    size_match = re.search(r"(\d+)\s*평", body_text)
    request_url_match = re.search(r"https?://cafe\.naver\.com/pcarpenter/(\d+)", body_text)

    return {
        "location": extract(r"공사지역\s*[:：]\s*(.+)"),
        "deadline": extract(r"공사희망일\s*[:：]\s*(.+)"),
        "company": extract(r"지정\s*열린업체명\s*[:：]\s*(.+)"),
        "size_pyeong": int(size_match.group(1)) if size_match else 0,
        "body_text": body_text,
        "request_url": request_url_match.group(0) if request_url_match else "",
        "image_urls": collect_image_urls_from_html(html),
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
