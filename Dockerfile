FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts

RUN mkdir -p /app/data /app/uploads \
 && chmod +x /app/scripts/entrypoint.sh

VOLUME ["/app/data", "/app/uploads"]

COPY .env.example /app/.env.example

# TODO: confirm mindcode owner/namespace
LABEL org.opencontainers.image.title="TimeHub" \
      org.opencontainers.image.description="Zentrale Zeiterfassung – Erfassung, Import, Export, Reporting" \
      org.opencontainers.image.vendor="mindsquare AG" \
      org.opencontainers.image.source="https://mindcode.mindsquare.de/joschka.rick/timehub" \
      org.opencontainers.image.version="2.0.0" \
      de.mindsquare.agenthub.category="productivity"

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
