"""박목수 열린견적서 카페 크롤러 — Selenium 브라우저 자동화 (오케스트레이션 레이어).

페이지 이동/로그인/iframe 진입만 이 모듈이 담당하고, 받아온 HTML에서 실제 데이터를
뽑아내는 로직은 전부 pipeline/crawl_parsing.py·crawl_region.py로 위임한다 — 그 쪽은
Selenium 없이 고정 HTML로 단위테스트가 가능하지만, 이 모듈은 실제 브라우저가 있어야
동작해서 자동화 테스트 대상이 아니다.

로그인은 여전히 수동이다 (자동 로그인은 하지 않음 — 네이버 계정 보안/약관 이슈).

출력 구조는 pipeline/ingest.py의 process_article()이 기대하는 것과 동일:
    {BASE_DIR}/
    └── {지역}/
        └── {article_id}/
            ├── {article_id}_0.jpg
            └── {article_id}.json
"""

import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from pipeline.crawl_download import download_images
from pipeline.crawl_parsing import (
    extract_article_id,
    extract_pcarpenter_links,
    is_valid_estimate_image,
    parse_article_rows,
    parse_estimate_detail,
    parse_estimate_link,
    parse_request_body,
    upgrade_image_url,
)
from pipeline.crawl_region import detect_region

CAFE_ID = "17593353"
BASE_DIR = Path("./estimate_data")
MAX_PAGES = 50
SLEEP_SEC = 1.5


def get_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    driver = webdriver.Chrome(options=options)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def naver_login(driver):
    """네이버 로그인 페이지를 열고, 사람이 직접 로그인을 완료할 때까지 대기한다."""
    driver.get("https://nid.naver.com/nidlogin.login")
    print("=" * 50)
    print("브라우저에서 네이버 로그인을 완료한 후")
    print("이 터미널에서 Enter 키를 누르세요.")
    print("=" * 50)
    input()
    print("로그인 확인 완료\n")


def enter_iframe(driver, timeout: int = 10) -> bool:
    try:
        WebDriverWait(driver, timeout).until(
            EC.frame_to_be_available_and_switch_to_it("cafe_main")
        )
        time.sleep(1.5)
        return True
    except Exception:
        driver.switch_to.default_content()
        return False


def get_user_articles(
    driver,
    member_hash: str,
    max_pages: int = MAX_PAGES,
    existing_ids: set | None = None,
    max_new: int | None = None,
) -> list[dict]:
    """유저 게시글 목록 수집. max_new에 도달하면 페이지 탐색을 조기 종료한다."""
    base_url = f"https://cafe.naver.com/f-e/cafes/{CAFE_ID}/members/{member_hash}"
    all_articles: list[dict] = []
    new_count = 0

    for page in range(1, max_pages + 1):
        driver.get(f"{base_url}?page={page}")

        if not enter_iframe(driver):
            print(f"  [WARN] iframe 진입 실패 (page {page}) -> 수집 종료")
            break

        page_articles = parse_article_rows(driver.page_source)
        driver.switch_to.default_content()

        if not page_articles:
            print(f"  더 이상 게시글 없음 (page {page}) -> 수집 종료")
            break

        all_articles.extend(page_articles)

        if existing_ids is not None and max_new is not None:
            for article in page_articles:
                aid = extract_article_id(article["url"])
                if aid and aid not in existing_ids:
                    new_count += 1

        print(
            f"  페이지 {page}: {len(page_articles)}건 (누적 {len(all_articles)}건"
            + (f", 신규 {new_count}건)" if max_new else ")")
        )

        if max_new is not None and new_count >= max_new:
            print(f"  목표 신규 {max_new}건 달성 -> 탐색 종료")
            break

        time.sleep(SLEEP_SEC)

    return all_articles


def get_estimate_link_from_post(driver, article_url: str) -> str | None:
    driver.get(article_url)
    if not enter_iframe(driver):
        return None
    link = parse_estimate_link(driver.page_source)
    driver.switch_to.default_content()
    return link


def get_estimate_detail(driver, estimate_url: str, retry: int = 2) -> dict | None:
    for attempt in range(1, retry + 1):
        driver.get(estimate_url)
        if not enter_iframe(driver):
            print(f"    [WARN] iframe 진입 실패 (시도 {attempt}/{retry})")
            time.sleep(3)
            continue

        html = driver.page_source
        driver.switch_to.default_content()

        detail = parse_estimate_detail(html)
        if detail is not None:
            return detail

        print(f"    [WARN] 본문 비어있음 (시도 {attempt}/{retry}), 3초 후 재시도...")
        time.sleep(3)

    return None


def get_request_body(driver, request_url: str) -> str:
    driver.get(request_url)

    try:
        WebDriverWait(driver, 3).until(EC.alert_is_present())
        driver.switch_to.alert.accept()
        return ""
    except Exception:
        pass

    if not enter_iframe(driver):
        return ""

    text = parse_request_body(driver.page_source)
    driver.switch_to.default_content()
    return text


def collect_linked_images(driver, body_text: str) -> list[str]:
    """본문 내 pcarpenter 링크를 전부 방문해 이미지를 추가로 모은다.

    래퍼 포스트(열린견적서 목록)가 실제 견적 이미지를 링크로만 가리킬 때 대응.
    """
    linked_urls = extract_pcarpenter_links(body_text)
    if not linked_urls:
        return []

    seen: set[str] = set()
    all_image_urls: list[str] = []

    for url in linked_urls:
        try:
            driver.get(url)

            try:
                driver.switch_to.alert.dismiss()
                print(f"    [SKIP] 삭제된 게시글: {url}")
                continue
            except Exception:
                pass

            if not enter_iframe(driver):
                driver.switch_to.default_content()
                continue

            from bs4 import BeautifulSoup

            soup = BeautifulSoup(driver.page_source, "html.parser")
            for img in soup.select("img"):
                src = img.get("src") or img.get("data-lazy-src") or img.get("data-src") or ""
                if not src or ("postfiles.pstatic.net" not in src and "cafeptthumb" not in src):
                    continue
                if not is_valid_estimate_image(src):
                    continue
                upgraded = upgrade_image_url(src)
                if upgraded not in seen:
                    seen.add(upgraded)
                    all_image_urls.append(upgraded)

            driver.switch_to.default_content()
        except Exception as e:
            print(f"    [ERR] 링크 방문 실패 {url}: {e}")
            try:
                driver.switch_to.default_content()
            except Exception:
                pass
        time.sleep(SLEEP_SEC)

    return all_image_urls


def _existing_article_ids(base_dir: Path = BASE_DIR) -> set[str]:
    """이미 수집된 article_id 목록 반환 (재실행 시 skip용)."""
    ids: set[str] = set()
    base = Path(base_dir)
    if not base.exists():
        return ids
    for region_dir in base.iterdir():
        if not region_dir.is_dir():
            continue
        for article_dir in region_dir.iterdir():
            if not article_dir.is_dir():
                continue
            if (article_dir / f"{article_dir.name}.json").exists():
                ids.add(article_dir.name)
    return ids


def crawl_user(
    member_hash: str,
    max_pages: int = MAX_PAGES,
    max_articles: int = 100,
    base_dir: Path = BASE_DIR,
) -> list[dict]:
    """member_hash 유저의 게시글을 전부 순회해 견적 데이터를 수집·저장한다."""
    base_dir = Path(base_dir)
    driver = get_driver()

    try:
        naver_login(driver)

        existing_ids = _existing_article_ids(base_dir)
        print(f"[INFO] 기존 수집 항목: {len(existing_ids)}개 (skip 대상)\n")

        print("게시글 목록 수집 중...\n")
        articles = get_user_articles(
            driver, member_hash, max_pages, existing_ids=existing_ids, max_new=max_articles
        )
        print(f"\n{len(articles)}건 게시글 처리 시작\n")

        results: list[dict] = []
        failed: list[dict] = []

        for i, article in enumerate(articles):
            print(f"[{i + 1}/{len(articles)}] {article['title'][:50]}")

            estimate_link = get_estimate_link_from_post(driver, article["url"])
            if not estimate_link:
                print("  [SKIP] 의뢰글 링크 없음 -> 건너뜀\n")
                continue

            article_id = extract_article_id(estimate_link) or str(i)
            if article_id in existing_ids:
                print(f"  [SKIP] 이미 수집됨: {article_id}\n")
                continue

            detail = get_estimate_detail(driver, estimate_link)
            if detail is None:
                print("  [FAIL] 본문 수집 실패 -> skip\n")
                failed.append({"article_id": article_id, "estimate_url": estimate_link})
                continue

            if detail.get("request_url"):
                detail["request_body_text"] = get_request_body(driver, detail["request_url"])
                time.sleep(SLEEP_SEC)
            else:
                detail["request_body_text"] = ""

            if detail.get("body_text"):
                linked_imgs = collect_linked_images(driver, detail["body_text"])
                if linked_imgs:
                    existing_urls = set(detail.get("image_urls", []))
                    added = [u for u in linked_imgs if u not in existing_urls]
                    detail["image_urls"] = detail.get("image_urls", []) + added

            region = detect_region(detail)
            article_dir = base_dir / region / str(article_id)
            print(f"  지역: {region}")

            if detail.get("image_urls"):
                saved = download_images(detail["image_urls"], article_dir, str(article_id))
                detail["local_images"] = [str(p) for p in saved]
            else:
                detail["local_images"] = []

            record = {
                "article_id": article_id,
                "region": region,
                "post_title": article["title"],
                "post_url": article["url"],
                "estimate_url": estimate_link,
                **detail,
            }

            article_dir.mkdir(parents=True, exist_ok=True)
            json_path = article_dir / f"{article_id}.json"
            json_path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            results.append(record)
            existing_ids.add(article_id)
            print(f"  완료 (이미지 {len(detail['local_images'])}장 저장)\n")
            time.sleep(SLEEP_SEC)

        print("=" * 50)
        print(f"전체 완료: {len(results)}건 수집")
        if failed:
            print(f"[FAIL] 수집 실패 {len(failed)}건:")
            for f_item in failed:
                print(f"  - {f_item['article_id']}  {f_item['estimate_url']}")
        print("=" * 50)

        return results

    finally:
        driver.quit()
