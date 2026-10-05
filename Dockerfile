FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/aggregator.db

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 容器启动即由 FastAPI lifespan 幂等执行 schema.sql 初始化数据库
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
