# Production image for the Notes API server. Build from the repository root, because the server needs
# openapi.yaml at runtime:  docker build -t notes-api:dev .
#
# Two stages on the same digest-pinned Python image: the builder installs the locked dependencies and the
# project into /app/.venv with uv, the runtime copies that venv plus the contract and the Alembic scripts.
# There is no ENTRYPOINT, so the same image runs migrations with a different command:
#   docker run --rm -e DATABASE_URL=... <image> alembic -c /app/alembic.ini upgrade head

FROM ghcr.io/astral-sh/uv:0.11.7@sha256:240fb85ab0f263ef12f492d8476aa3a2e4e1e333f7d67fbdd923d00a506a516a AS uv

FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS builder
COPY --from=uv /uv /bin/uv
# Precompile bytecode (the runtime filesystem is read-only), copy instead of hardlinking out of the cache
# mount, use the image's interpreter, and leave the dev dependency group out.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0 UV_NO_DEV=1
WORKDIR /app
# Dependencies first, from the lockfile alone, so this layer survives source edits.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=server/uv.lock,target=uv.lock \
    --mount=type=bind,source=server/pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project
# README.md is pyproject's `readme`, which the uv_build backend requires when it builds the project.
COPY server/pyproject.toml server/uv.lock server/README.md ./
COPY server/src ./src
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-editable

# The runtime must be the same image as the builder: the venv records the interpreter's absolute path.
FROM python:3.12.14-slim-trixie@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea
LABEL org.opencontainers.image.source=https://github.com/timothy/notes \
      org.opencontainers.image.licenses=Apache-2.0
# Apply Debian security updates published since the pinned base was built, then drop the apt lists.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
# Everything below is root-owned and read-only to the app user; the process writes nothing.
COPY --from=builder /app/.venv ./.venv
COPY server/alembic.ini ./alembic.ini
COPY server/alembic ./alembic
# Last, so a contract edit invalidates no layer above it. CONTRACT_PATH points the server at it.
COPY openapi.yaml ./openapi.yaml
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    CONTRACT_PATH=/app/openapi.yaml
USER 10001:10001
EXPOSE 8000
# Exec form: uvicorn is PID 1, installs its own SIGTERM handler, and drains for up to 20 s. One worker per
# container; scale with replicas. DATABASE_URL must be supplied at run time.
CMD ["uvicorn", "--factory", "notes_api.main:create_app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "20"]
