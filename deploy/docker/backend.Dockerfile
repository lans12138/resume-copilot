FROM python:3.12.14-slim-bookworm AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /workspace

FROM base AS development

COPY requirements-dev.lock ./
RUN python -m pip install --no-cache-dir --requirement requirements-dev.lock

# The runtime lock is not *installed* here, but it has to be *present*: the
# ADR-0001 tripwire asserts that the runtime dependency set declares no
# LangGraph, and it reads the file relative to the repo root
# (tests/unit/test_fin009_adr_contract.py). Without it the case skips inside
# this image -- the one place CI runs the suite -- so the guard would never
# fire in CI for the single file that actually defines the runtime
# dependencies, while passing on a host checkout. Copying it is what makes
# "green in CI" mean the same thing as "green locally".
COPY requirements.lock ./

COPY apps ./apps
COPY backend ./backend
COPY tests ./tests
COPY scripts ./scripts
COPY alembic.ini ./
COPY migrations ./migrations
COPY pyproject.toml ./
# The repository's design documents are part of the contract, not decoration:
# tests/unit/test_fin009_adr_contract.py asserts that README and the design
# documents still describe the self-built RunEngine, and it reads them relative
# to the repo root. Without them the suite passes on a host checkout and fails
# inside this image, which is the one place CI runs it.
COPY docs ./docs
COPY *.md ./

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
