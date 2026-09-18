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
import shutil
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from pipeline.crawl_download import download_images, download_pdf_attachment
from pipeline.crawl_parsing import (
    ESTIMATE_IMAGE_DOMAINS,
    collect_image_urls_from_html,
    extract_article_id,
    extract_pcarpenter_links,
    extract_size_pyeong,
    parse_article_rows,
    parse_estimate_detail,
    parse_estimate_link,
    parse_request_body,
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


def _scroll_through_content(driver, step: int = 1200, pause: float = 0.4, max_scrolls: int = 20) -> None:
    """지연 로딩(lazy-load)되는 본문 이미지가 실제 src로 채워지도록 아래로 스크롤한다.

    스마트에디터 게시글은 뷰포트에 들어와야 placeholder 대신 진짜 이미지 URL을 채우는
    경우가 많아서, 스크롤 없이 곧장 page_source를 읽으면 화면 아래쪽(보통 견적서 이미지
    본문)의 <img src>가 비어있거나 서명 배지처럼 이미 로딩된 이미지만 잡히고 만다.
    """
    for _ in range(max_scrolls):
        at_bottom = driver.execute_script(
            "return (window.scrollY + window.innerHeight) >= document.body.scrollHeight - 5;"
        )
        if at_bottom:
            break
        driver.execute_script(f"window.scrollBy(0, {step});")
        time.sleep(pause)
    time.sleep(pause)


def _wait_for_estimate_content(driver, timeout: int = 5) -> None:
    """견적서 이미지 또는 PDF 첨부 링크가 DOM에 나타날 때까지 짧게 폴링한다.

    "첨부파일 모아보기" 같은 첨부 섹션은 본문과 별개로 비동기 로딩되는 경우가 있어서
    스크롤과 무관하게 캡처 시점에 아직 안 붙어있을 수 있다 — 고정 sleep 대신 실제로
    나타날 때까지 기다리고, 텍스트만 있는 정상 게시글이면 타임아웃으로 조용히 넘어간다.
    """
    domain_checks = " || ".join(
        f'!!document.querySelector(\'img[src*="{d}"]\')' for d in ESTIMATE_IMAGE_DOMAINS
    )
    script = f"return !!document.querySelector('a.se-file-save-button') || {domain_checks};"
    try:
        WebDriverWait(driver, timeout).until(lambda d: d.execute_script(script))
    except Exception:
        pass


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
        for attempt in range(1, 3):
            driver.get(f"{base_url}?page={page}")
            if enter_iframe(driver):
                break
            print(f"  [WARN] iframe 진입 실패 (page {page}, 시도 {attempt}/2)")
            time.sleep(3)
        else:
            print(f"  [WARN] iframe 진입 반복 실패 (page {page}) -> 수집 종료")
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


def resolve_estimate_content(driver, article_url: str) -> tuple[str | None, dict | None]:
    """목록 항목(article_url) 자체가 이미 완성된 열린견적서 답변글인지 확인한다.

    업체마다 목록 항목의 구조가 다르다 — 어떤 업체(강원1호 등)는 목록 항목이 짧은
    인덱스 글이고 실제 견적서 내용은 본문 안 pcarpenter 링크를 따라가야 나온다. 반면
    다른 업체(경북3호 등)는 목록 항목 자체가 이미지·PDF까지 포함된 완성된 답변글이고,
    본문 안 pcarpenter 링크는 "고객 의뢰글"을 참고용으로 보여주는 카드일 뿐이다.

    이 둘을 구분 안 하고 무조건 "본문 안 첫 pcarpenter 링크를 따라간다"로 처리하면
    후자 케이스에서 진짜 답변글 대신 참고용 카드(고객 원본 글)로 잘못 새서, 이미지가
    있는 정상 게시글인데도 0장으로 수집되는 문제가 생긴다(실측: 경북3호 계정에서 확인).

    article_url 자체를 먼저 파싱해서 이미지/PDF가 있으면 그 글을 그대로 쓰고, 없으면
    기존처럼 본문 안 링크를 따라간다. 반환: (estimate_url, 이미 파싱된 detail 또는 None).
    """
    driver.get(article_url)
    if not enter_iframe(driver):
        return None, None

    _scroll_through_content(driver)
    _wait_for_estimate_content(driver)
    html = driver.page_source
    driver.switch_to.default_content()

    detail = parse_estimate_detail(html)
    if detail and (detail.get("image_urls") or detail.get("pdf_attachment_url")):
        return article_url, detail

    link = parse_estimate_link(html)
    return link, None


def get_estimate_detail(driver, estimate_url: str, retry: int = 2) -> dict | None:
    for attempt in range(1, retry + 1):
        driver.get(estimate_url)
        if not enter_iframe(driver):
            print(f"    [WARN] iframe 진입 실패 (시도 {attempt}/{retry})")
            time.sleep(3)
            continue

        _scroll_through_content(driver)
        _wait_for_estimate_content(driver)
        html = driver.page_source
        driver.switch_to.default_content()

        detail = parse_estimate_detail(html)
        if detail is not None:
            return detail

        print(f"    [WARN] 본문 비어있음 (시도 {attempt}/{retry}), 3초 후 재시도...")
        time.sleep(3)

    return None


def get_request_body(driver, request_url: str, retry: int = 2) -> str:
    for attempt in range(1, retry + 1):
        driver.get(request_url)

        try:
            WebDriverWait(driver, 3).until(EC.alert_is_present())
            driver.switch_to.alert.accept()
            return ""
        except Exception:
            pass

        if not enter_iframe(driver):
            print(f"    [WARN] 의뢰글 iframe 진입 실패 (시도 {attempt}/{retry})")
            time.sleep(3)
            continue

        _scroll_through_content(driver)
        text = parse_request_body(driver.page_source)
        driver.switch_to.default_content()
        if text:
            return text

        print(f"    [WARN] 의뢰글 본문 비어있음 (시도 {attempt}/{retry}), 3초 후 재시도...")
        time.sleep(3)

    return ""


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
        found: list[str] = []
        for attempt in range(1, 3):
            try:
                driver.get(url)

                try:
                    driver.switch_to.alert.dismiss()
                    print(f"    [SKIP] 삭제된 게시글: {url}")
                    break
                except Exception:
                    pass

                if not enter_iframe(driver):
                    print(f"    [WARN] 링크 iframe 진입 실패 {url} (시도 {attempt}/2)")
                    driver.switch_to.default_content()
                    time.sleep(3)
                    continue

                _scroll_through_content(driver)
                _wait_for_estimate_content(driver)
                found = collect_image_urls_from_html(driver.page_source)

                driver.switch_to.default_content()
            except Exception as e:
                print(f"    [ERR] 링크 방문 실패 {url} (시도 {attempt}/2): {e}")
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
                time.sleep(3)
                continue

            if found:
                break
            print(f"    [WARN] 링크에서 이미지 못 찾음 {url} (시도 {attempt}/2), 재시도...")
            time.sleep(3)

        for u in found:
            if u not in seen:
                seen.add(u)
                all_image_urls.append(u)
        time.sleep(SLEEP_SEC)

    return all_image_urls


def _find_existing_record(base_dir: Path, article_id: str) -> dict | None:
    """article_id가 어느 지역 폴더에든 이미 저장돼 있으면 그 레코드를 반환한다.

    재크롤링 함수(crawl_single_article/crawl_specific_articles)는 목록 순회 없이
    estimate_url만 받아서 post_title을 모른다 — 빈 문자열로 저장하면 detect_region()의
    판정 후보 하나가 통째로 빠져서 지역 판정 품질이 떨어진다. 기존 레코드가 있으면
    거기서 post_title/post_url을 되살려 쓴다.
    """
    base = Path(base_dir)
    if not base.exists():
        return None
    for region_dir in base.iterdir():
        if not region_dir.is_dir():
            continue
        json_path = region_dir / str(article_id) / f"{article_id}.json"
        if not json_path.exists():
            continue
        try:
            return json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _remove_stale_duplicate_dirs(base_dir: Path, article_id: str, keep_region: str) -> None:
    """이번에 판정된 지역(keep_region)과 다른 지역 폴더에 같은 article_id가 남아있으면
    지운다.

    재크롤링 때 지역 판정이 이전과 달라지면 새 지역 폴더에만 저장되고 옛 폴더가 고아로
    남는데, run_ingest가 지역 폴더를 전부 훑다 보니 같은 article_id를 두 번 집어서
    배치 API에 custom_id 중복으로 거부당하는 문제가 실측으로 확인됐다. article_id당
    폴더가 항상 하나만 존재하도록 강제한다.
    """
    base = Path(base_dir)
    if not base.exists():
        return
    for region_dir in base.iterdir():
        if not region_dir.is_dir() or region_dir.name == keep_region:
            continue
        stale_dir = region_dir / str(article_id)
        if stale_dir.exists():
            print(f"    [INFO] 고아 폴더 정리: {stale_dir} (지역이 {region_dir.name} -> {keep_region}로 변경됨)")
            shutil.rmtree(stale_dir)


def _existing_article_ids(base_dir: Path = BASE_DIR) -> set[str]:
    """이미 수집된 article_id 목록 반환 (재실행 시 skip용).

    이미지가 0장 저장된 채 끝난 article_id는 skip 대상에서 제외한다 — iframe 진입
    실패/지연 로딩 타이밍 문제로 이미지를 못 건진 실패 케이스일 수 있어서, 다음 실행에서
    재시도할 기회를 준다. json 자체가 없거나 파싱할 수 없는 경우도 동일하게 재시도 대상.
    """
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
            json_path = article_dir / f"{article_dir.name}.json"
            if not json_path.exists():
                continue
            try:
                record = json.loads(json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if record.get("local_images"):
                ids.add(article_dir.name)
    return ids


def collect_and_save_article(
    driver,
    estimate_link: str,
    article_id: str,
    post_title: str,
    post_url: str,
    base_dir: Path,
    prefetched_detail: dict | None = None,
) -> dict | None:
    """estimate_link 하나를 파싱·다운로드·저장까지 끝낸다 (목록 순회와 무관하게 재사용
    가능 — crawl_user()의 루프와 crawl_single_article() 둘 다 이 함수를 쓴다).

    prefetched_detail이 있으면(resolve_estimate_content()가 이미 본문을 파싱해둔 경우)
    같은 페이지를 또 불러오지 않고 그대로 쓴다.

    실패(본문 수집 실패)하면 None.
    """
    if not post_title:
        existing = _find_existing_record(base_dir, article_id)
        if existing:
            post_title = existing.get("post_title", "") or post_title
            post_url = existing.get("post_url", "") or post_url

    detail = prefetched_detail if prefetched_detail is not None else get_estimate_detail(driver, estimate_link)
    if detail is None:
        return None

    if detail.get("request_url"):
        detail["request_body_text"] = get_request_body(driver, detail["request_url"])
        time.sleep(SLEEP_SEC)
    else:
        detail["request_body_text"] = ""

    # 강원1호 스타일 래퍼 포스트는 평수가 본문(body_text)엔 없고 링크된 고객 의뢰글
    # (request_body_text)에만 있는 경우가 많다 — 방금 받아온 request_body_text로 재시도.
    if not detail.get("size_pyeong") and detail.get("request_body_text"):
        detail["size_pyeong"] = extract_size_pyeong(detail["request_body_text"])

    # body_text가 업체 광고 문구뿐이고 평수는 목록 제목에만 있는 경우도 있다(예: "26평형").
    # post_title은 parse_estimate_detail()이 모르는 정보라 여기서 마지막으로 재시도한다.
    if not detail.get("size_pyeong") and post_title:
        detail["size_pyeong"] = extract_size_pyeong(post_title)

    # 이미 이 글 자체에서 이미지/PDF를 찾았으면 링크를 따라갈 필요가 없다 — 래퍼 포스트가
    # 견적 이미지를 링크로만 가리킬 때만(자기 콘텐츠가 없을 때만) 필요한 보강 단계다.
    if detail.get("body_text") and not detail.get("image_urls") and not detail.get("pdf_attachment_url"):
        linked_imgs = collect_linked_images(driver, detail["body_text"])
        if linked_imgs:
            existing_urls = set(detail.get("image_urls", []))
            added = [u for u in linked_imgs if u not in existing_urls]
            detail["image_urls"] = detail.get("image_urls", []) + added

    region = detect_region({**detail, "post_title": post_title})
    article_dir = base_dir / region / str(article_id)
    print(f"  지역: {region}")

    _remove_stale_duplicate_dirs(base_dir, article_id, keep_region=region)

    if detail.get("image_urls"):
        saved = download_images(detail["image_urls"], article_dir, str(article_id))
        detail["local_images"] = [str(p) for p in saved]
    else:
        detail["local_images"] = []

    if detail.get("pdf_attachment_url"):
        pdf_path, fail_reason = download_pdf_attachment(
            detail["pdf_attachment_url"], article_dir, str(article_id), driver.get_cookies()
        )
        if pdf_path:
            detail["local_images"].append(str(pdf_path))
        else:
            print(f"    [WARN] PDF 첨부 다운로드 실패: {fail_reason}")

    record = {
        "article_id": article_id,
        "region": region,
        "post_title": post_title,
        "post_url": post_url,
        "estimate_url": estimate_link,
        **detail,
    }

    article_dir.mkdir(parents=True, exist_ok=True)
    json_path = article_dir / f"{article_id}.json"
    json_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"  완료 (article_id={article_id}, 이미지 {len(detail['local_images'])}장 저장)\n")
    return record


def crawl_single_article(estimate_url: str, base_dir: Path = BASE_DIR) -> dict | None:
    """estimate_url 하나만 크롤링한다 (목록 페이지 순회 없이) — 특정 article_id 재현/재시도용.

    이미 수집됐는지(existing_ids) 여부는 확인하지 않는다 — 명시적으로 하나를 다시 받고
    싶어서 부르는 함수라 항상 새로 받아서 덮어쓴다.
    """
    base_dir = Path(base_dir)
    article_id = extract_article_id(estimate_url)
    if article_id is None:
        print("[FAIL] estimate_url에서 article_id를 못 찾음")
        return None

    driver = get_driver()
    try:
        naver_login(driver)
        record = collect_and_save_article(
            driver, estimate_url, article_id, post_title="", post_url="", base_dir=base_dir
        )
        if record is None:
            print("[FAIL] 본문 수집 실패")
        return record
    finally:
        driver.quit()


def crawl_specific_articles(estimate_urls: list[str], base_dir: Path = BASE_DIR) -> list[dict]:
    """estimate_url 목록을 한 번의 로그인 세션으로 순서대로 재수집한다.

    parse_estimate_link 버그(사이드바 링크 오인식) 등으로 오염이 의심되는 article_id를
    감사 스크립트로 뽑은 뒤, 그 목록 전체를 --article-url처럼 매번 로그인하지 않고
    한 번에 재수집할 때 쓴다. crawl_single_article()과 달리 로그인을 한 번만 한다.
    """
    base_dir = Path(base_dir)
    driver = get_driver()
    results: list[dict] = []

    try:
        naver_login(driver)

        for i, url in enumerate(estimate_urls):
            article_id = extract_article_id(url)
            if article_id is None:
                print(f"[{i + 1}/{len(estimate_urls)}] [FAIL] article_id 추출 실패: {url}")
                continue

            print(f"[{i + 1}/{len(estimate_urls)}] article_id={article_id}")
            record = collect_and_save_article(
                driver, url, article_id, post_title="", post_url="", base_dir=base_dir
            )
            if record is None:
                print("  [FAIL] 본문 수집 실패\n")
                continue
            results.append(record)
            time.sleep(SLEEP_SEC)

        print("=" * 50)
        print(f"전체 완료: {len(results)}/{len(estimate_urls)}건 재수집")
        print("=" * 50)
        return results
    finally:
        driver.quit()


def crawl_from_post_urls(items: list[dict], base_dir: Path = BASE_DIR) -> tuple[list[dict], list[str]]:
    """오염 의심 레코드를 post_url부터(estimate_url이 아니라) 다시 완전히 재해석한다.

    items: [{"post_url": ..., "post_title": ..., "old_article_id": ...}, ...]

    감사 스크립트(audit_title_content_mismatch류)로 찾은 레코드들은 estimate_url 자체가
    틀렸다(예전 크롤러가 페이지 전환 직후 아직 안 바뀐 이전 글을 그대로 캡처한 것으로
    추정 — 실측: 여러 건에서 article_id[N]의 post_url이 article_id[N-1]과 정확히 일치하는
    연쇄 패턴 확인됨). estimate_url을 그대로 재사용하는 crawl_specific_articles와 달리,
    이 함수는 post_url부터 resolve_estimate_content()를 다시 돌려서 진짜 estimate_link를
    처음부터 재도출한다.

    old_article_id는 새로 도출된 article_id와 다를 수 있다(예전엔 엉뚱한 번호였으니까) —
    호출자가 반환값의 "old_article_id_to_remove" 목록으로 옛 폴더/Mongo 문서를 정리해야
    한다. 이 함수는 로컬 파일만 다루고 Mongo는 안 건드린다.
    """
    base_dir = Path(base_dir)
    driver = get_driver()
    results: list[dict] = []
    stale_ids: list[str] = []

    try:
        naver_login(driver)

        for i, item in enumerate(items):
            post_url = item["post_url"]
            post_title = item.get("post_title", "")
            old_article_id = item.get("old_article_id")
            print(f"[{i + 1}/{len(items)}] {post_title[:50]} (old={old_article_id})")

            estimate_link, prefetched_detail = resolve_estimate_content(driver, post_url)
            if not estimate_link:
                print("  [SKIP] 의뢰글 링크 없음\n")
                continue

            article_id = extract_article_id(estimate_link)
            if article_id is None:
                print("  [FAIL] article_id 추출 실패\n")
                continue

            record = collect_and_save_article(
                driver, estimate_link, article_id, post_title, post_url, base_dir,
                prefetched_detail=prefetched_detail,
            )
            if record is None:
                print("  [FAIL] 본문 수집 실패\n")
                continue

            results.append(record)
            if old_article_id and old_article_id != article_id:
                stale_ids.append(old_article_id)
                print(f"  [INFO] article_id 변경: {old_article_id} -> {article_id} (옛 폴더 정리 필요)")
            time.sleep(SLEEP_SEC)

        print("=" * 50)
        print(f"전체 완료: {len(results)}/{len(items)}건 재수집")
        if stale_ids:
            print(f"정리 필요한 옛 article_id: {stale_ids}")
        print("=" * 50)
        return results, stale_ids
    finally:
        driver.quit()


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

            estimate_link, prefetched_detail = resolve_estimate_content(driver, article["url"])
            if not estimate_link:
                print("  [SKIP] 의뢰글 링크 없음 -> 건너뜀\n")
                continue

            article_id = extract_article_id(estimate_link) or str(i)
            if article_id in existing_ids:
                print(f"  [SKIP] 이미 수집됨: {article_id}\n")
                continue

            record = collect_and_save_article(
                driver,
                estimate_link,
                article_id,
                article["title"],
                article["url"],
                base_dir,
                prefetched_detail=prefetched_detail,
            )
            if record is None:
                print("  [FAIL] 본문 수집 실패 -> skip\n")
                failed.append({"article_id": article_id, "estimate_url": estimate_link})
                continue

            results.append(record)
            existing_ids.add(article_id)
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
