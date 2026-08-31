"""검증 결과의 신뢰도로 어느 컬렉션에 저장할지 결정한다.

"신뢰도를 어떻게 계산하는지"(pipeline/validators.py)와 "그 신뢰도로 뭘 할지"를
분리한다 — 임계값을 조정하거나 저장 정책이 바뀔 때 계산 로직을 안 건드려도 되게.
"""

from typing import Literal

CONFIDENCE_THRESHOLD = 0.7

Destination = Literal["estimate_cases", "review_queue"]


def route_case(confidence: float) -> Destination:
    return "estimate_cases" if confidence >= CONFIDENCE_THRESHOLD else "review_queue"
