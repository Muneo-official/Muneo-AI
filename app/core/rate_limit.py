from fastapi import Request
from slowapi import Limiter

# Spring이 내부 프록시로 붙는 구조라(01_시스템_구조.md), 클라이언트 IP는 전부 Spring의 IP로 뭉친다.
# 그래서 IP가 아니라 실제 최종 사용자를 식별하는 x-user-id 헤더로 rate limit을 건다.
# 헤더가 없는 요청(잘못된 클라이언트)은 전부 "anonymous" 하나로 묶여서 자연히 강하게 제한된다.


def _rate_limit_key(request: Request) -> str:
    return request.headers.get("x-user-id", "anonymous")


def _global_key(request: Request) -> str:
    """x-user-id 검증이 없으므로, 헤더 값을 계속 바꿔가며 사용자당 제한을 우회하는 걸 막는
    전역 상한용 키 — 모든 요청이 같은 버킷에 몰린다."""
    return "global"


limiter = Limiter(key_func=_rate_limit_key, default_limits=["60/minute"])
