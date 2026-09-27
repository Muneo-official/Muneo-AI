// scripts/bench/load_test.js — 리스크 진단 동시 부하 테스트 (k6, Vision mock 서버 전용).
//
// 실행 (프로젝트 루트에서, mock 서버를 먼저 띄운 뒤):
//   k6 run -e CASE=S3 --out json=logs/bench/k6_raw.json --summary-export=logs/bench/k6_summary.json scripts/bench/load_test.js
//   python -m scripts.bench.k6_result --label baseline      # 결과를 report.py 형식으로 변환 + 서버 로그 집계
//
// 부하 단계는 시나리오 하나당 한 단계이고, 시간차를 두고 순서대로 돈다 (k6 1회 실행 = 전체 단계).
// 이 엔드포인트는 요청 하나가 수 분 걸린다 (베이스라인 S3 약 196초). 그래서 VU를 수십 초 간격으로
// 올리는 일반적인 ramping 대신, 단계마다 이전 단계 요청이 다 끝날 시간을 둔다 — 안 그러면 단계 경계에서
// 진행 중인 요청이 끊기거나(interrupted) 다음 단계와 섞인다.
//
// 부하 모델 (-e MODEL=closed|open)
//   closed (기본): 동시 사용자 LEVELS명이 각자 ITERATIONS번 요청 (응답을 받아야 다음 요청). 시나리오 이름 c<N>
//   open:          분당 RATES건이 응답과 무관하게 STAGE 동안 도착. 못 따라가면 대기가 쌓여 응답시간이
//                  계속 늘고, VU가 모자라면 dropped_iterations로 드러난다. 시나리오 이름 r<N>
//
// 옵션 (-e KEY=VALUE)
//   CASE=S3             logs/bench/cases.json의 케이스 id
//   BASE_URL            기본 http://127.0.0.1:8000
//   LEVELS=1,5,10,20    closed: 동시 사용자 수 단계
//   ITERATIONS=2        closed: 사용자당 요청 수
//   RATES=1,2,4         open: 분당 도착 건수 단계
//   STAGE=8m            closed: 단계 최대 시간(maxDuration) / open: 도착이 이어지는 시간
//   P95_MS=120000       단계별 p95 합격 기준 (임시 목표 2분 — 제품 목표치가 정해지면 바꾼다)
//   ALLOW_REAL=1        mock이 아닌 서버에도 실행 (비용 발생 — 기본은 거부)
//
// 이미지를 같은 필드명(files)으로 여러 장 보내야 해서 jslib FormData를 쓴다 (k6 기본 multipart는
// 같은 이름의 파일 배열을 못 보낸다). FormData는 문자열을 바이트 문자열로 취급하므로, 한글 폼 값은
// UTF-8 바이트 문자열로 미리 바꿔 넣는다 — 안 그러면 "아파트"가 깨져 422가 난다.

import http from 'k6/http';
import exec from 'k6/execution';
import { check } from 'k6';
import { FormData } from 'https://jslib.k6.io/formdata/0.0.2/index.js';

const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000';
const MODEL = __ENV.MODEL || 'closed';
const CASE_ID = __ENV.CASE || 'S3';
const STAGE = __ENV.STAGE || (MODEL === 'open' ? '5m' : '8m');
const P95_MS = Number(__ENV.P95_MS || 120000);

// open()의 상대 경로는 이 스크립트 위치 기준이다 → 프로젝트 루트 = ../../
const ROOT = '../../';
const isAbsolute = (p) => p.startsWith('/') || /^[A-Za-z]:/.test(p);
const CASES_FILE = __ENV.CASES_FILE || `${ROOT}logs/bench/cases.json`;
const CASE = JSON.parse(open(CASES_FILE)).cases.find((c) => c.id === CASE_ID);
if (!CASE) throw new Error(`케이스 ${CASE_ID}가 ${CASES_FILE}에 없습니다`);

const IMAGES = CASE.images.map((path) => ({
  name: path.split('/').pop(),
  mime: path.toLowerCase().endsWith('.png') ? 'image/png' : 'image/jpeg',
  data: open(isAbsolute(path) ? path : ROOT + path, 'b'),
}));

const utf8 = (s) => unescape(encodeURIComponent(String(s)));
const toSeconds = (d) => {
  const m = /^(\d+(?:\.\d+)?)(s|m|h)$/.exec(d);
  if (!m) throw new Error(`시간 형식 오류: ${d} (예: 30s, 8m)`);
  return Number(m[1]) * { s: 1, m: 60, h: 3600 }[m[2]];
};

// 단계 사이 간격: closed는 maxDuration 이후 gracefulStop(30s) + 여유 30s,
// open은 도착이 끝난 뒤 진행 중 요청이 끝날 때까지 gracefulStop(= STAGE) + 여유 30s
const GRACEFUL = MODEL === 'open' ? STAGE : '30s';
const SLOT_S = toSeconds(STAGE) + toSeconds(GRACEFUL) + 30;

function buildScenarios() {
  const steps = (MODEL === 'open' ? __ENV.RATES || '1,2,4' : __ENV.LEVELS || '1,5,10,20')
    .split(',').map(Number);
  const scenarios = {};
  steps.forEach((n, i) => {
    const common = { startTime: `${i * SLOT_S}s`, gracefulStop: GRACEFUL };
    if (MODEL === 'open') {
      scenarios[`r${n}`] = {
        executor: 'constant-arrival-rate', rate: n, timeUnit: '1m', duration: STAGE,
        preAllocatedVUs: n * 6 + 2, maxVUs: n * 10 + 5, ...common,
      };
    } else {
      scenarios[`c${n}`] = {
        executor: 'per-vu-iterations', vus: n, iterations: Number(__ENV.ITERATIONS || 2),
        maxDuration: STAGE, ...common,
      };
    }
  });
  return scenarios;
}

const scenarios = buildScenarios();
const thresholds = {};
for (const name of Object.keys(scenarios)) {
  thresholds[`http_req_duration{scenario:${name}}`] = [`p(95)<${P95_MS}`];
  thresholds[`http_req_failed{scenario:${name}}`] = ['rate<0.01'];
}

export const options = {
  scenarios,
  thresholds,
  tags: { case: CASE_ID },
  summaryTrendStats: ['min', 'med', 'avg', 'p(90)', 'p(95)', 'max'],
};

export function setup() {
  const res = http.get(`${BASE_URL}/bench/info`);
  const info = res.status === 200 ? res.json() : null;
  if (!info) exec.test.abort(`${BASE_URL}/bench/info 응답 없음 — scripts.bench.server로 띄운 서버인지 확인하세요`);
  if (info.vision !== 'mock' && !__ENV.ALLOW_REAL) {
    exec.test.abort('서버가 mock 모드가 아닙니다 — 실제 Vision API로 부하를 걸면 비용이 크고 Anthropic rate limit에 '
      + '먼저 막힙니다. python -m scripts.bench.server --mock-vision <결과 JSON>으로 띄우세요.');
  }
  console.log(`[bench] ${CASE_ID} (${CASE.total_chunks}청크) · ${MODEL} · 단계 ${Object.keys(scenarios).join(', ')} `
    + `· 단계 간격 ${SLOT_S}s · 예상 소요 약 ${Math.ceil((SLOT_S * Object.keys(scenarios).length) / 60)}분 · mock ${info.latency_source}`);
}

export default function () {
  const fd = new FormData();
  for (const [key, value] of Object.entries(CASE.form)) fd.append(key, utf8(value));
  for (const img of IMAGES) fd.append('files', http.file(img.data, img.name, img.mime));

  const res = http.post(`${BASE_URL}/risk-detector/analyze`, fd.body(), {
    headers: { 'Content-Type': `multipart/form-data; boundary=${fd.boundary}` },
    timeout: '30m',
  });
  check(res, { 'status is 200': (r) => r.status === 200 });
}
