# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/root/.cache/huggingface \
    FASTEMBED_CACHE_PATH=/root/.cache/fastembed

WORKDIR /app

# libgomp1：fastembed/ONNX Runtime；curl：容器健康检查。
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

# 先安装锁定依赖，再补 requirements.txt 中不在旧 lock 内的运行时依赖（如 python-docx）。
COPY requirements.txt requirements.lock ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.lock -r requirements.txt

COPY . .

RUN mkdir -p /app/chroma_db /app/logs /app/rag/data /root/.cache/fastembed /root/.cache/huggingface

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
