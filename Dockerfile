# dartboard bot. Built and started by compose.yaml: docker compose up -d --build
# pinned: the same base on every rebuild; bump the version and digest on purpose (docker buildx imagetools inspect)
FROM python:3.13.18-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

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
