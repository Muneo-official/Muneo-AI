"""박목수 열린견적서 카페 크롤러 실행 진입점.
"""

import argparse
import json

from pipeline.crawler import (
    BASE_DIR,
    MAX_PAGES,
    crawl_from_post_urls,
    crawl_single_article,
    crawl_specific_articles,
    crawl_user,
)

DEFAULT_MEMBER_HASH = "i7RciwNrHZ1a9Iu-KZTFIXm-fcu-p95nmShQno8CedA"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--member-hash", type=str, default=DEFAULT_MEMBER_HASH)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--max-articles", type=int, default=100)
    parser.add_argument("--base-dir", type=str, default=str(BASE_DIR))
    parser.add_argument(
        "--article-url",
        type=str,
        default=None,
        help="목록 순회 없이 estimate_url 하나만 재크롤링 (특정 article_id 재시도/검증용). "
        "기존 json의 estimate_url 필드를 그대로 넘기면 됨.",
    )
    parser.add_argument(
        "--article-urls-file",
        type=str,
        default=None,
        help="estimate_url 여러 개를 한 번의 로그인 세션으로 재크롤링. "
        "감사 스크립트가 뽑은 suspect_link_mismatch.json처럼 "
        '[{"estimate_url": "..."}, ...] 형태거나 URL 문자열 리스트인 JSON 파일을 받는다.',
    )
    parser.add_argument(
        "--post-urls-file",
        type=str,
        default=None,
        help="오염 의심 레코드를 post_url부터 처음부터 재해석해서 재크롤링. "
        '[{"post_url": "...", "post_title": "...", "old_article_id": "..."}, ...] 형태의 '
        "JSON 파일을 받는다(audit_title_content_mismatch류 감사 스크립트 출력). "
        "estimate_url을 그대로 재사용하는 --article-urls-file과 달리 resolve_estimate_content()를 "
        "처음부터 다시 돌린다 — estimate_url 자체가 틀렸던 레코드용.",
    )
    args = parser.parse_args()

    if args.article_url:
        record = crawl_single_article(args.article_url, base_dir=args.base_dir)
        print(f"[DONE] {'1건 수집' if record else '수집 실패'} -> {args.base_dir}")
        return

    if args.article_urls_file:
        with open(args.article_urls_file, encoding="utf-8") as f:
            items = json.load(f)
        urls = [item["estimate_url"] if isinstance(item, dict) else item for item in items]
        results = crawl_specific_articles(urls, base_dir=args.base_dir)
        print(f"[DONE] {len(results)}/{len(urls)}건 재수집 -> {args.base_dir}")
        return

    if args.post_urls_file:
        with open(args.post_urls_file, encoding="utf-8") as f:
            items = json.load(f)
        results, stale_ids = crawl_from_post_urls(items, base_dir=args.base_dir)
        print(f"[DONE] {len(results)}/{len(items)}건 재수집 -> {args.base_dir}")
        if stale_ids:
            stale_path = str(args.post_urls_file) + ".stale_ids.json"
            with open(stale_path, "w", encoding="utf-8") as f:
                json.dump(stale_ids, f, ensure_ascii=False, indent=2)
            print(f"[INFO] article_id가 바뀐 옛 레코드 {len(stale_ids)}건 -> {stale_path} (정리 필요)")
        return

    results = crawl_user(
        member_hash=args.member_hash,
        max_pages=args.max_pages,
        max_articles=args.max_articles,
        base_dir=args.base_dir,
    )
    print(f"[DONE] {len(results)}건 수집 -> {args.base_dir}")


if __name__ == "__main__":
    main()
