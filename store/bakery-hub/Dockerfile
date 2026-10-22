# ステージ1: 荷造り場（builder）
FROM python:3.12-slim AS builder

WORKDIR /app

# コンパイルに必要な道具だけ、このステージだけに入れる
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ステージ2: 本番トラック
FROM python:3.12-slim

WORKDIR /app

# psycopg2-binaryの実行に必要なランタイムライブラリだけ入れる（gccは不要）
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# builderで作った"完成品"だけ積み替え
COPY --from=builder /root/.local /root/.local
COPY . .

ENV PATH=/root/.local/bin:$PATH

EXPOSE 5000

CMD ["sh", "-c", "python startup_guard.py && exec gunicorn --bind 0.0.0.0:${PORT:-5000} app:app"]
