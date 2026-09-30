"""
scripts/bench/server.py — 벤치마크 전용 서버 실행 (운영 코드에 테스트용 플래그를 넣지 않으려고 분리).

기본 모드(real): 운영과 같은 앱을 rate limit만 끈 채 띄운다. 단건 측정(bench_latency)이
5/min 제한에 걸려 429가 섞이는 걸 막기 위해서다. Vision API는 실제로 호출된다(비용 발생).

real 모드에선 파싱 결과도 캡처한다: Anthropic 클라이언트를 감싸 청크별 원본 모델 출력(tool_use input)을,
RiskAnalyzer.analyze를 감싸 룰 분석에 들어간 최종 항목을 request_id별로 모아 두고
GET /bench/capture/{request_id}로 내준다(한 번 가져가면 지운다). 출력 스키마를 바꾸는 비용 작업에서
공종별 금액 비교·필드 제거 영향 분석에 쓴다(docs/RISK_DETECTOR_COST_LOG.md). 응답 자체는 바꾸지 않는다.

mock 모드(--mock-vision): Anthropic 클라이언트만 가짜로 바꾼다. 실측 결과 파일의 호출별 지연시간 중
하나를 무작위로 골라 그만큼 기다린 뒤 고정 파싱 결과를 돌려준다 — 실제 API가 느린 모양은
그대로 재현하되 비용 0, Anthropic rate limit 영향 0. 이미지 전처리·룰 분석·Mongo 검색·
리랭킹은 실제 코드를 그대로 탄다. 동시 부하 테스트(k6, load_test.js)는 이 모드에서만 돌린다.

--risk-schema full: 리스크 진단이 축소 스키마(RISK_ESTIMATE_TOOL) 대신 수집용 전체 스키마를 보내게 한다.
스키마 축소 전후를 같은 코드에서 번갈아 재려는 것 — 기준 측정을 위해 코드를 되돌릴 필요가 없다.

--parse-cache: 이미지 파싱 캐시(risk_parse_cache)를 켠다. 기본은 꺼짐 — 켜두면 같은 이미지 반복 측정의
2회차부터 Vision을 안 불러 측정이 틀어진다. 캐시 히트 자체를 잴 때만 켠다(real 모드 전용, .env의 Mongo에 저장됨).

--model: 리스크 진단 파싱 모델만 바꾼다(vision_client.RISK_MODEL). 모델별 요청 형식 차이(강제 도구 호출 불가 모델의
tool_choice·thinking)는 build_risk_api_params가 처리한다. 운영과 다른 모델은 --parse-cache·--risk-schema full과 병용 불가.

사용법:
  python -m scripts.bench.server                                   # real
  python -m scripts.bench.server --risk-schema full                # real, 축소 전 스키마(기준 측정용)
  python -m scripts.bench.server --parse-cache                     # real, 파싱 캐시 켬(캐시 히트 측정용)
  python -m scripts.bench.server --model claude-haiku-4-5          # real, 리스크 파싱 모델 교체(모델 비교용)
  python -m scripts.bench.server --mock-vision logs/bench/latency_baseline_*.json
  python -m scripts.bench.server --mock-vision-fixed 20            # 실측 전, 고정 20초
"""

import argparse
import asyncio
import copy
import json
import os
import pathlib
import random
import time
from collections import defaultdict
from types import SimpleNamespace

import uvicorn
from fastapi import HTTPException

from app.core.config import get_settings
from app.core.logging import get_request_id
from app.core.rate_limit import limiter
from app.domain.risk_analyzer import RiskAnalyzer
from app.main import app
from pipeline import vision_client
from scripts.bench.common import PRICING_PER_MTOK, params_image_digest

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


def _fake_response() -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", input=_MOCK_PARSE_RESULT)],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0),
    )


def _install_mock(latencies: list[float], seed: int) -> None:
    """Anthropic 클라이언트 자체를 가짜로 바꾼다 — 서비스가 어느 호출 함수를 쓰든 실제 API에 닿지 않게.

    (처음엔 서비스 모듈의 호출 함수를 바꿔치기했는데, 서비스가 다른 함수를 쓰도록 바뀌면 mock이
    조용히 빠지고 실제 API가 호출된다 — 병렬화하며 동기→비동기 호출로 바꿀 때 실제로 그럴 뻔했다.)
    """
    rng = random.Random(seed)

    async def async_create(**params):
        await asyncio.sleep(rng.choice(latencies))  # 비동기 클라이언트처럼 스레드를 점유하지 않고 기다린다
        return _fake_response()

    def sync_create(**params):
        time.sleep(rng.choice(latencies))
        return _fake_response()

    vision_client._async_client = SimpleNamespace(messages=SimpleNamespace(create=async_create))
    vision_client._client = SimpleNamespace(messages=SimpleNamespace(create=sync_create))
    # 이중 안전장치: 혹시 실제 클라이언트가 새로 만들어져도 인증 실패로 끝나고 과금되지 않게
    os.environ["ANTHROPIC_API_KEY"] = "bench-mock-mode-no-real-calls"


_CAPTURE: dict[str, dict] = defaultdict(lambda: {"vision_calls": [], "line_items": None})


def _install_capture() -> None:
    """실제 클라이언트를 감싸 호출은 그대로 보내고 결과만 옆에 적어 둔다 (외부 경계에서 가로챈다 — mock과 같은 이유).

    RiskAnalyzer.analyze는 클래스에서 감싼다 — 서비스 인스턴스가 언제 만들어지든 적용되도록.
    """
    real = vision_client.get_async_client()

    async def create(**params):
        response = await real.messages.create(**params)
        request_id = get_request_id()  # gather로 만든 태스크도 요청의 contextvar를 복사해 간다
        if request_id:
            output = next((b.input for b in response.content if b.type == "tool_use"), None)
            _CAPTURE[request_id]["vision_calls"].append({
                "chunk_digest": params_image_digest(params),
                "output": copy.deepcopy(output),
                "output_tokens": response.usage.output_tokens,
            })
        return response

    vision_client._async_client = SimpleNamespace(messages=SimpleNamespace(create=create))

    original_analyze = RiskAnalyzer.analyze

    def analyze(self, line_items):
        request_id = get_request_id()
        if request_id:
            _CAPTURE[request_id]["line_items"] = copy.deepcopy(line_items)
        return original_analyze(self, line_items)

    RiskAnalyzer.analyze = analyze


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock-vision", type=pathlib.Path, help="지연 분포를 가져올 bench_latency 결과 JSON")
    group.add_argument("--mock-vision-fixed", type=float, help="고정 지연(초)으로 mock")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--risk-schema", choices=("risk", "full"), default="risk",
                        help="리스크 진단 출력 스키마: risk=축소(운영과 같음), full=수집용 전체(축소 전 기준 측정용)")
    parser.add_argument("--parse-cache", action="store_true",
                        help="이미지 파싱 캐시를 켠다(운영과 같음). 기본은 꺼짐 — 같은 이미지로 반복 측정하면 2회차부터 "
                             "Vision을 안 불러서 지연·비용·정확도 측정이 틀어지기 때문")
    parser.add_argument("--model", choices=sorted(PRICING_PER_MTOK), default=vision_client.RISK_MODEL,
                        help="리스크 진단 파싱 모델 (기본: 운영과 같음). 모델 비교 측정용")
    args = parser.parse_args()
    if args.model != vision_client.RISK_MODEL and args.parse_cache:
        # 캐시 키의 파싱 버전은 서버 시작 시 운영 모델로 계산돼 있어, 다른 모델 결과가 운영 키로 저장된다
        parser.error("--model(운영과 다른 모델)은 --parse-cache와 같이 쓸 수 없습니다")
    if args.model != vision_client.RISK_MODEL and args.risk_schema == "full":
        # full 스키마 경로(build_api_params)는 수집용 MODEL을 쓰므로 --model이 조용히 무시된다
        parser.error("--model(운영과 다른 모델)은 --risk-schema full과 같이 쓸 수 없습니다")
    if args.parse_cache and args.risk_schema == "full":
        # 캐시 키의 파싱 버전은 운영 스키마 기준이라, full로 바꿔도 키가 같아 축소 스키마 결과가 섞인다
        parser.error("--parse-cache는 --risk-schema full과 같이 쓸 수 없습니다")
    if args.parse_cache and (args.mock_vision or args.mock_vision_fixed is not None):
        # mock의 고정 파싱 결과가 실제 이미지 해시 키로 Mongo 캐시에 저장되면, 이후 운영에서 그 가짜 결과가 나간다
        parser.error("--parse-cache는 mock 모드와 같이 쓸 수 없습니다")

    # lifespan(app.core.deps)이 서버 시작 시 설정을 읽으므로 그 전에 환경변수로 덮는다 (.env보다 우선)
    os.environ["RISK_PARSE_CACHE_ENABLED"] = "true" if args.parse_cache else "false"
    get_settings.cache_clear()

    limiter.enabled = False
    # build_risk_api_params가 호출 시점에 모듈 값을 읽으므로 여기서 바꾸면 리스크 경로 요청만 바뀐다
    vision_client.RISK_MODEL = args.model
    if args.risk_schema == "full":
        # acall_vision_api_with_usage가 모듈 전역 이름으로 찾으므로 여기서 바꾸면 리스크 경로 요청만 바뀐다
        vision_client.build_risk_api_params = vision_client.build_api_params
    if args.mock_vision:
        latencies = _latencies_from_results(args.mock_vision)
        _install_mock(latencies, args.seed)
        mode = {"vision": "mock", "latency_source": str(args.mock_vision), "latency_samples": len(latencies)}
    elif args.mock_vision_fixed is not None:
        _install_mock([args.mock_vision_fixed], args.seed)
        mode = {"vision": "mock", "latency_source": f"fixed {args.mock_vision_fixed}s", "latency_samples": 1}
    else:
        _install_capture()
        mode = {"vision": "real", "model": args.model, "capture": True, "risk_schema": args.risk_schema,
                "parse_cache": args.parse_cache}

    @app.get("/bench/info")
    async def bench_info() -> dict:
        return {"rate_limit": "disabled", **mode}

    @app.get("/bench/capture/{request_id}")
    async def bench_capture(request_id: str) -> dict:
        if request_id not in _CAPTURE:
            raise HTTPException(404, "캡처 없음 — real 모드가 아니거나 이미 가져간 요청")
        return _CAPTURE.pop(request_id)

    print(f"[bench server] {mode} · rate limit 비활성 · http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
