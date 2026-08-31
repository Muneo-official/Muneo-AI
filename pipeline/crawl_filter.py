"""크롤링 단계 사전 필터 — 견적서가 아닌 이미지를 저비용으로 걸러낸다.

실제 크롤링 데이터(1,626개 이미지)를 해시로 분석한 결과, **40.3%(656개)가 단 14개의
바이트 단위로 동일한 파일**이었다(pipeline/results/crawl_prefilter.md 참고). 로고·
프로필뱃지·완성 견본 사진·무관한 스톡 사진이 같은 파일 그대로 여러 게시글에 반복
첨부되고 있었다 — 실제 견적서 내용은 게시글마다 다를 수밖에 없으니 다른 게시글과
바이트가 완전히 같은 이미지는 사실상 확실하게 보일러플레이트다(오탐 위험 거의 없음).

이 방식(해시 블록리스트)을 선택한 이유: 크기·비율 같은 휴리스틱은 애매한 경계 케이스가
있어 진짜 견적서를 잘못 거를 위험(false negative)이 있지만, "다른 게시글과 완전히
동일한 파일"이라는 조건은 실제 견적서 내용상 성립할 수 없는 조건이라 안전하다.
"""

import hashlib
from collections import Counter

# 실제 크롤링 데이터(estimate_data, 1,626개 이미지)에서 3회 이상 반복 확인된 해시.
# pipeline/results/crawl_prefilter.md의 조사 방법으로 재현·갱신 가능 — 새 크롤링 배치가
# 쌓이면 find_boilerplate_hashes()로 다시 뽑아서 이 목록을 갱신한다.
KNOWN_BOILERPLATE_HASHES: frozenset[str] = frozenset({
    "b2c50579088776936c71ca21eff97e74ba35b764ac9661a1178d1a6474f01ab7",  # 250x238, 127회 반복
    "59784af67c09975830d05bdafb228dee2f203d1b03099739e9f63f46aa6cb0d5",  # 218x218, 110회 반복
    "4772f8536578e2af142a1b54f837b056b458c5a926c23e7bcefccc26108caec5",  # 180x180, 93회 반복
    "d15d8f7fd79a5ddcc4de833f6dcc8e2eb014282e9de67c8057b00634f44308e8",  # 1600x2133, 71회 반복
    "1de5af7d1e03a1818fb905c7f41b743810801cb4499e693779855e9ae42b81fd",  # 1600x1067, 60회 반복
    "ef036b89f13ccb8ce9ed246d84dc31aa6037433d4ab5986451da3d8ef246ba7e",  # 300x300, 55회 반복
    "063da3a8493f5bdd523de51ebef5accb935254b1d205b6465038a905da173228",  # 290x290, 48회 반복
    "d4cf1fee06d6c313f74d44ae25e93be8048a5dea907971c4805a43008869ad9a",  # 157x157, 27회 반복
    "6975577f6620fe8c69344ca7ead46764291c0340f769e977c7f32dc0ec2a93cf",  # 500x500, 23회 반복
    "0d748d80e6dd4231d28e83129e145c8535fa968cdbb001eb79125931d741fe4d",  # 720x961, 22회 반복
    "65dfa6ad54b463f6f296a59bdaedf846129a19ec73a67bc221f20efa7a465f4d",  # 1600x2133, 11회 반복
    "9e3f3d5df53f4ccdf104c39e853b451ff0e3de2b619812a1ad45358365e41f6c",  # 198x179, 3회 반복
    "8c55e45739b6ce83531b3a62b2cb77297c83ea7dbe7213886cbaaf7fc5982798",  # 1179x2556, 3회 반복
    "b1ff4baefb4e3353c2b6da8f88e7cfdb2d239c7e3728dfab7e641a2c49468ecd",  # 607x500, 3회 반복
})


def compute_image_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def is_boilerplate(path: str, known_hashes: frozenset[str] = KNOWN_BOILERPLATE_HASHES) -> bool:
    """이 이미지가 알려진 보일러플레이트(로고·뱃지·완성 견본 사진 등)와 바이트 단위로
    동일한지 확인한다. True면 파싱(Vision API 호출) 없이 건너뛰어도 된다."""
    return compute_image_hash(path) in known_hashes


def find_boilerplate_hashes(image_paths: list[str], min_occurrences: int = 3) -> set[str]:
    """여러 게시글에 걸쳐 min_occurrences회 이상 바이트 단위로 반복되는 해시를 찾는다.

    KNOWN_BOILERPLATE_HASHES를 처음 만들 때 쓴 방법 그대로 — 새 크롤링 배치가 쌓이면
    이 함수로 다시 스캔해서 KNOWN_BOILERPLATE_HASHES 갱신 여부를 판단한다.
    """
    counts: Counter[str] = Counter(compute_image_hash(p) for p in image_paths)
    return {h for h, n in counts.items() if n >= min_occurrences}
