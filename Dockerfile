# 災害リスク診断 — コンテナ定義
# Cloud Run / Fly.io / Render など、コンテナを動かせる環境で共通に使える。
FROM python:3.12-slim

WORKDIR /app

# 地図の凡例に日本語フォントが必要（無いと凡例が省略される）
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY mock ./mock

# 既定はテンプレート文。コンテナ内に Ollama は無いため。
# Claude API を使う場合は EXAPP_LLM_PROVIDER=anthropic と ANTHROPIC_API_KEY を渡す。
ENV EXAPP_LLM_PROVIDER=none \
    EXAPP_UI=1 \
    PORT=8080

EXPOSE 8080

# PaaS 側が $PORT を指定してくるので、それに従う
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
