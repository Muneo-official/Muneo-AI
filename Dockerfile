FROM python:3.12-slim

# 모델 캐시를 이미지 안 고정 경로에 둔다. 기본값(~/.cache)에 두면 root로 받은 캐시를
# 비특권 유저가 못 읽고, 런타임에 조용히 재다운로드가 일어난다.
ENV HF_HOME=/opt/huggingface \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# torch는 반드시 CPU 전용 인덱스에서 받는다. 이 줄을 빼면 PyPI 기본 휠(CUDA 포함, 2.5GB+)이
# sentence-transformers의 의존성으로 딸려와 이미지가 몇 배로 커진다.
COPY requirements.txt .
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# 모델을 빌드 타임에 구워둔다. 예전엔 컨테이너 최초 기동 시 HF에서 받았는데, 캐시 볼륨이
# 없어서 컨테이너가 새로 뜰 때마다 매번 다시 받았다 (기동 지연 + HF 가용성에 배포가 종속).
#
# USE_RERANKER=true로 빌드할 때만 리랭커(약 2.2GB)를 같이 굽는다. 기본 빌드는
# 임베딩 모델(약 460MB)만 포함하므로 이미지가 그만큼 작다.
# 런타임 동작도 같은 값으로 맞춰지므로, 리랭커 없는 이미지가 리랭커를 켜고 뜨는 일은 없다.
#
# 아래 모델명은 app/core/config.py의 embed_model / reranker_model 기본값과 일치해야 한다.
# 런타임에 EMBED_MODEL 등을 다른 값으로 덮어쓰면 구워둔 캐시가 안 맞아 다시 받게 된다.
ARG USE_RERANKER=false
ENV USE_RERANKER=${USE_RERANKER}
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('paraphrase-multilingual-MiniLM-L12-v2')" \
    && if [ "$USE_RERANKER" = "true" ]; then \
         python -c "from sentence_transformers import CrossEncoder; CrossEncoder('Dongjin-kr/ko-reranker', max_length=512)"; \
       fi \
    && chmod -R a+rX /opt/huggingface

# scripts/도 같이 넣는다 — expire_estimates / accuracy_report / setup_indexes를
# 컨테이너 안에서(배치잡·수동 실행) 돌릴 수 있어야 한다.
COPY app ./app
COPY scripts ./scripts

RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# 모델 로드 때문에 기동에 시간이 걸린다. 오케스트레이터 쪽 startup probe도
# 넉넉히(리랭커 ON이면 특히) 잡아야 기동 중 재시작 루프에 빠지지 않는다.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health').read()"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
