# dartboard bot. Built and started by compose.yaml: docker compose up -d --build
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Seoul

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install . \
 && useradd --create-home --uid 10001 dartboard \
 && mkdir -p /app/data && chown dartboard /app/data

USER dartboard
CMD ["python", "-m", "dartboard"]
