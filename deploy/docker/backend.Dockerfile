FROM python:3.12.14-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

FROM base AS development

COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements-dev.lock

COPY apps ./apps
COPY backend ./backend
COPY tests ./tests
COPY scripts ./scripts
COPY alembic.ini ./
COPY migrations ./migrations
COPY pyproject.toml ./

CMD ["pytest", "-q"]

FROM base AS runtime

COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements.lock

COPY apps ./apps
COPY backend ./backend
COPY alembic.ini ./
COPY migrations ./migrations

EXPOSE 8000

CMD ["uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
