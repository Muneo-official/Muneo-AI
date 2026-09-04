"""박목수 열린견적서 카페 크롤러 실행 진입점.
"""

import argparse

from pipeline.crawler import BASE_DIR, MAX_PAGES, crawl_user

DEFAULT_MEMBER_HASH = "i7RciwNrHZ1a9Iu-KZTFIXm-fcu-p95nmShQno8CedA"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--member-hash", type=str, default=DEFAULT_MEMBER_HASH)
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    parser.add_argument("--max-articles", type=int, default=100)
    parser.add_argument("--base-dir", type=str, default=str(BASE_DIR))
    args = parser.parse_args()

    results = crawl_user(
        member_hash=args.member_hash,
        max_pages=args.max_pages,
        max_articles=args.max_articles,
        base_dir=args.base_dir,
    )
    print(f"[DONE] {len(results)}건 수집 -> {args.base_dir}")


if __name__ == "__main__":
    main()
