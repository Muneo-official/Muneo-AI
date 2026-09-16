"""estimate_cases/review_queue에서 post_title과 실제 본문 내용이 어긋난 레코드를 찾는다.

배경: 예전 크롤러가 게시글 페이지 전환 직후, 새 콘텐츠가 로딩되기 전에 이전 페이지를
그대로 캡처하는 race condition이 있었다(article_id[N]의 post_url이 article_id[N-1]과
일치하는 연쇄 패턴으로 실측 확인 — eval/results/reranker_hybrid_eval.md "코퍼스 오염
693건(81%) 발견 및 정리" 참고). 이번 세션에서 크롤러 자체(스크롤+대기,
resolve_estimate_content())는 고쳤지만, 이미 쌓인 오염 데이터는 크롤링을 새로 돌릴
때마다 다시 생길 수 있는 문제가 아니라 완전히 다른 종류의 버그가 또 생길 수도 있다 —
그래서 "제목의 구체적인 내용이 본문에 없다"는 근본 신호로 감사하는 이 스크립트를 매
크롤링+파싱 배치 뒤에 돌리는 걸 루틴화한다.

감사 방법: post_title에서 3글자 이상 구체 토큰(아파트/단지명, 동 이름 등 — "서울시"
같은 광역 지명은 제외)을 뽑아 body_text/request_body_text 어디에도 하나도 안 나타나면
의심으로 분류한다. 광역 지명만 일치하는 건 증거로 안 친다 — 같은 지역 다른 고객 글에도
흔히 나오기 때문이다(872731/857571 사례에서 실측 확인 — "인천시"는 일치해도 "중산동"/
"우미리2차" 같은 구체 지명은 전혀 다른 경우).

실행:
    python -m scripts.audit_content_mismatch                # 감사만, 의심 목록 저장
    python -m scripts.audit_content_mismatch --delete        # 의심 레코드를 Mongo+로컬에서 삭제(재크롤링 전까지 코퍼스에서 뺌)

삭제 후 재크롤링:
    python -m scripts.run_crawler --post-urls-file pipeline/results/suspect_title_content_mismatch.json

한계: 형태소 분석 없이 정규식 토큰화만 쓰기 때문에(이 프로젝트 방침 — konlpy/mecab
미사용), "확인하실"/"바랍니다" 같은 일반 서술어가 가끔 STOPWORDS를 빠져나가 오탐을
낼 수 있다. --delete로 실제 삭제하기 전에 저장된 목록에서 몇 건 샘플로 실제 오염이
맞는지 확인하는 걸 권장한다(그동안 실측 오탐률은 낮았지만 0은 아니었음).
"""

import argparse
import asyncio
import json
import re
import shutil
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import get_settings

load_dotenv()

OUT_PATH = Path(__file__).resolve().parent.parent / "pipeline" / "results" / "suspect_title_content_mismatch.json"
ESTIMATE_DATA_DIR = Path("estimate_data")

TAG_RE = re.compile(r"\[[^\]]*\]|\([^)]*\)")
STOPWORDS = {
    "아파트", "리모델링", "열린견적서", "견적서", "고객", "고객님", "견적", "비용",
    "상담", "의뢰", "공사", "인테리어", "전체", "부분", "긴급", "방문", "수정본",
    "님의", "번님", "리모델링공사", "공사비", "견적문의", "문의",
    "부탁드립니다", "요청합니다", "드립니다", "견적요청", "견적요청합니다", "의뢰합니다",
}
GENERIC_REGION_SUFFIXES = ("시", "도", "군", "구")

# "경기14호"/"강원1호"/"인천5호" 등 업체 서명·광고 문구에 반복적으로 등장하는 줄 —
# 실제 프로젝트 내용과 섞여 있을 수 있어(예: 857571은 진짜 견적 내용 옆에 이 문구도
# 같이 있었음) 통째로 "판단 불가" 처리하면 안 되고, 이 줄들만 걷어내고 남은 내용으로
# 판단해야 한다(실측: 848476/849496처럼 이미지만 있고 본문은 광고 문구뿐인 정상
# 게시글을 마커 존재만으로 걸러내려다 진짜 불일치까지 같이 걸러질 뻔했음).
BOILERPLATE_LINE_PATTERNS = [
    re.compile(p) for p in (
        r"시공갤러리", r"성공적인 인테리어 공사를 위한 안내서", r"친절한 상담을 원하시는",
        r"열린업체명은 반드시", r"링크 주소로 신청", r"^★+$", r"바로가기$",
        r"pcarpenter\.co\.kr", r"me2\.do", r"goo\.gl", r"업체평가", r"우수업체 선정",
        r"열린업체 활동지역", r"승강기이용료", r"주민동의서대행료", r"현장방문 실측",
        r"견적가는 변동 가능", r"작업일수 기준", r"시공 가능한 지역", r"지역만 가능합니다",
        r"이점 양해",
    )
]


def strip_boilerplate(text: str) -> str:
    lines = (text or "").splitlines()
    kept = [ln for ln in lines if not any(p.search(ln) for p in BOILERPLATE_LINE_PATTERNS)]
    return "\n".join(kept)


def has_comparable_content(body_text: str, request_body_text: str) -> bool:
    """광고 문구를 걷어낸 뒤에도 지역명 이상으로 구체적인 내용이 남아있는지."""
    combined = strip_boilerplate(body_text) + " " + strip_boilerplate(request_body_text)
    tokens = [t for t in distinctive_tokens(combined) if not is_generic_region_token(t)]
    return bool(tokens)


def is_generic_region_token(tok: str) -> bool:
    """"인천시"/"중구"처럼 넓은 행정구역명뿐인 토큰 — 같은 지역 다른 고객 글에도 흔히
    나와서 "일치"의 증거로 못 쓴다. "중산동"(동)이나 "우미리2차"(단지명)처럼 더 구체적인
    토큰만 진짜 신호로 본다."""
    return len(tok) <= 5 and tok[-1] in GENERIC_REGION_SUFFIXES


def distinctive_tokens(title: str) -> list[str]:
    """post_title에서 판단에 쓸 구체 토큰을 뽑는다 (3글자 이상, 태그/불용어 제외)."""
    t = TAG_RE.sub(" ", title or "")
    tokens = re.findall(r"[가-힣0-9]{3,}", t)
    return [tok for tok in tokens if tok not in STOPWORDS]


def is_mismatch(title: str, body_text: str, request_body_text: str) -> tuple[bool, list[str]]:
    """(의심 여부, 판단에 쓴 구체 토큰 목록). 구체 토큰이 없어 판단 불가면 (False, [])."""
    tokens = distinctive_tokens(title)
    specific_tokens = [tok for tok in tokens if not is_generic_region_token(tok)]
    if not specific_tokens:
        return False, []
    if not has_comparable_content(body_text, request_body_text):
        return False, []

    haystack = (body_text or "") + " " + (request_body_text or "")
    matched_specific = [tok for tok in specific_tokens if tok in haystack]
    return (not matched_specific), specific_tokens


async def find_suspects(col) -> list[dict]:
    suspects = []
    async for case in col.find({}, {"embedding": 0}):
        title = case.get("post_title") or ""
        if not title:
            continue
        mismatch, tokens = is_mismatch(title, case.get("body_text") or "", case.get("request_body_text") or "")
        if mismatch:
            suspects.append({
                "post_url": case.get("post_url"),
                "post_title": title,
                "old_article_id": case.get("article_id"),
                "region": case.get("region"),
                "tokens_checked": tokens,
            })
    return suspects


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--delete", action="store_true",
        help="의심 레코드를 Mongo(estimate_cases/review_queue)와 로컬 estimate_data에서 삭제. "
        "기본은 감사만 하고 삭제 안 함.",
    )
    args = parser.parse_args()

    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongo_uri)
    db = client[settings.mongo_db_name]
    cases_col = db["estimate_cases"]
    queue_col = db["review_queue"]

    suspects: list[dict] = []
    seen_ids: set[str] = set()
    for col in (cases_col, queue_col):
        for s in await find_suspects(col):
            if s["old_article_id"] not in seen_ids:
                seen_ids.add(s["old_article_id"])
                suspects.append(s)

    print(f"[INFO] 의심 레코드 {len(suspects)}건 발견")
    for s in suspects[:20]:
        print(f"  - {s['old_article_id']} ({s['region']}) {s['post_title']!r} tokens={s['tokens_checked']}")
    if len(suspects) > 20:
        print(f"  ... 외 {len(suspects) - 20}건")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(suspects, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[OK] 의심 목록 저장: {OUT_PATH}")

    if not args.delete or not suspects:
        client.close()
        return

    old_ids = [s["old_article_id"] for s in suspects]
    r1 = await cases_col.delete_many({"article_id": {"$in": old_ids}})
    r2 = await queue_col.delete_many({"article_id": {"$in": old_ids}})
    print(f"[OK] Mongo 삭제 — estimate_cases: {r1.deleted_count}건, review_queue: {r2.deleted_count}건")
    client.close()

    old_id_set = set(old_ids)
    deleted_dirs = 0
    if ESTIMATE_DATA_DIR.exists():
        for region_dir in list(ESTIMATE_DATA_DIR.iterdir()):
            if not region_dir.is_dir():
                continue
            for article_dir in list(region_dir.iterdir()):
                if article_dir.is_dir() and article_dir.name in old_id_set:
                    shutil.rmtree(article_dir)
                    deleted_dirs += 1
    print(f"[OK] 로컬 폴더 삭제: {deleted_dirs}개")
    print(f"[다음 단계] python -m scripts.run_crawler --post-urls-file {OUT_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
