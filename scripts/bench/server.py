"""
scripts/bench/server.py — 벤치마크 전용 서버 실행 (운영 코드에 테스트용 플래그를 넣지 않으려고 분리).

기본 모드(real): 운영과 같은 앱을 rate limit만 끈 채 띄운다. 단건 측정(bench_latency)이
5/min 제한에 걸려 429가 섞이는 걸 막기 위해서다. Vision API는 실제로 호출된다(비용 발생).

mock 모드(--mock-vision): Vision 호출만 가짜로 바꾼다. 실측 결과 파일의 호출별 지연시간 중
하나를 무작위로 골라 그만큼 sleep한 뒤 고정 파싱 결과를 돌려준다 — 실제 API가 느린 모양은
그대로 재현하되 비용 0, Anthropic rate limit 영향 0. 이미지 전처리·룰 분석·Mongo 검색·
리랭킹은 실제 코드를 그대로 탄다. 동시 부하 테스트(bench_load)는 이 모드에서만 돌린다.

사용법:
  python -m scripts.bench.server                                   # real
  python -m scripts.bench.server --mock-vision logs/bench/latency_baseline_*.json
  python -m scripts.bench.server --mock-vision-fixed 20            # 실측 전, 고정 20초
"""

import argparse
import json
import pathlib
import random
import time

import uvicorn

import app.domain.risk_detector_service as service_module
from app.core.rate_limit import limiter
from app.main import app
from pipeline.vision_client import MODEL, VisionCallResult

# 30평대 전체 리모델링 견적서에서 흔히 나오는 공종 구성 — 룰 분석·가격 체크가 실제처럼 돌도록
_MOCK_PARSE_RESULT = {
    "is_estimate": True,
    "total_cost": 42_000_000,
    "line_items": [
        {"category": c, "description": d, "amount": a}
        for c, d, a in [
            ("철거", "기존 마감재 철거", 2_500_000), ("철거", "폐기물 처리", 800_000),
            ("설비", "급배수 배관 교체", 1_800_000), ("전기", "조명 교체", 1_600_000),
            ("전기", "콘센트·스위치 교체", 700_000), ("목공", "천장 몰딩", 2_200_000),
            ("목공", "문틀 교체", 1_900_000), ("도배", "실크벽지 시공", 3_100_000),
            ("바닥", "강마루 시공", 5_400_000), ("타일", "주방 벽 타일", 900_000),
            ("욕실", "욕실 전체 리모델링", 7_800_000), ("가구", "주방 싱크대", 6_200_000),
            ("가구", "신발장", 1_100_000), ("창호", "샷시 교체", 4_300_000),
            ("공과잡비", "승강기 보양", 300_000), ("공과잡비", "준공 청소", 400_000),
        ]
    ],
}


def _latencies_from_results(path: pathlib.Path) -> list[float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    latencies = [
        call["latency_s"]
        for req in data["requests"]
        if req.get("server")
        for call in req["server"]["vision_calls"]
    ]
    if not latencies:
        raise SystemExit(f"{path}에 Vision 호출 기록이 없습니다 — bench_latency 결과 파일인지 확인하세요.")
    return latencies


def _install_mock(latencies: list[float], seed: int) -> None:
    rng = random.Random(seed)

    def mock_call(image_bytes: bytes, client=None) -> VisionCallResult:
        latency = rng.choice(latencies)
        time.sleep(latency)  # 실제 동기 클라이언트처럼 스레드를 붙잡는다 (threadpool 포화 재현)
        return VisionCallResult(
            result=_MOCK_PARSE_RESULT,
            latency_s=latency,
            input_tokens=0,
            output_tokens=0,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )

    service_module.call_vision_api_with_usage = mock_call
    service_module.get_client = lambda: None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock-vision", type=pathlib.Path, help="지연 분포를 가져올 bench_latency 결과 JSON")
    group.add_argument("--mock-vision-fixed", type=float, help="고정 지연(초)으로 mock")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    limiter.enabled = False
    if args.mock_vision:
        latencies = _latencies_from_results(args.mock_vision)
        _install_mock(latencies, args.seed)
        mode = {"vision": "mock", "latency_source": str(args.mock_vision), "latency_samples": len(latencies)}
    elif args.mock_vision_fixed is not None:
        _install_mock([args.mock_vision_fixed], args.seed)
        mode = {"vision": "mock", "latency_source": f"fixed {args.mock_vision_fixed}s", "latency_samples": 1}
    else:
        mode = {"vision": "real", "model": MODEL}

    @app.get("/bench/info")
    async def bench_info() -> dict:
        return {"rate_limit": "disabled", **mode}

    print(f"[bench server] {mode} · rate limit 비활성 · http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
