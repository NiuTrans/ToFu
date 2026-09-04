# syntax=docker/dockerfile:1.7

# Tofu container targets. The digest pins the multi-platform Python 3.12 slim
# index; dependency updates are controlled separately by the committed uv.lock.
ARG PYTHON_IMAGE=python:3.12-slim@sha256:2c941e860699f878900b0edc2403613c234d4b32eda3cc9fa7036991a2a63c4a

FROM ${PYTHON_IMAGE} AS python-dependencies
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY pyproject.toml uv.lock README.md ./
RUN python -m pip install --no-cache-dir uv==0.12.5 \
    && UV_PROJECT_ENVIRONMENT=/opt/tofu-api \
       UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
       uv sync --frozen --no-dev --no-install-project --extra app --extra scale \
    && UV_PROJECT_ENVIRONMENT=/opt/tofu-worker \
       UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
       uv sync --frozen --no-dev --no-install-project \
       --extra app --extra scale --extra worker \
    && UV_PROJECT_ENVIRONMENT=/opt/tofu-agent \
       UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
       uv sync --frozen --no-dev --no-install-project

# Build and install the public wheel into the dependency-only environment.
# This stage sees the source; the final agent image does not.
FROM python-dependencies AS agent-builder
COPY . .
RUN uv build --wheel --out-dir /wheelhouse \
    && uv pip install --python /opt/tofu-agent/bin/python --no-deps \
       /wheelhouse/tofu_agent-*.whl

FROM ${PYTHON_IMAGE} AS runtime-base
ARG TOFU_UID=10001
ARG TOFU_GID=10001
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${TOFU_GID}" tofu \
    && useradd --uid "${TOFU_UID}" --gid "${TOFU_GID}" \
       --create-home --shell /usr/sbin/nologin tofu
WORKDIR /app
COPY --chown=tofu:tofu . .
RUN mkdir -p /app/data/backups /app/logs /app/uploads \
    && chown -R tofu:tofu /app/data /app/logs /app/uploads
ENV PORT=15000 \
    BIND_HOST=0.0.0.0 \
    MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOFU_SERVER_WORKER=1 \
    TOFU_MANAGED_BY=docker
EXPOSE 15000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:15000/health/live || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "server.py"]

# API target: no compiler, PostgreSQL server, Playwright package, or browser.
FROM runtime-base AS api
COPY --from=python-dependencies --chown=tofu:tofu /opt/tofu-api /opt/tofu-api
ENV PATH=/opt/tofu-api/bin:$PATH
USER tofu:tofu

# Worker target: browser/tool runtimes are isolated from the serving image.
# Docker Compose targets this stage so the low-cost personal `all` role keeps
# the browser features historically shipped by the standalone image.
FROM runtime-base AS worker
COPY --from=python-dependencies --chown=tofu:tofu \
    /opt/tofu-worker /opt/tofu-worker
ENV PATH=/opt/tofu-worker/bin:$PATH \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
RUN /opt/tofu-worker/bin/python -m playwright install chromium --with-deps --only-shell \
    && chown -R tofu:tofu /opt/ms-playwright
USER tofu:tofu

# Public headless runtime: wheel + agent dependencies only. It contains no
# repository checkout, full Tofu application bundle, database driver, or app data.
FROM ${PYTHON_IMAGE} AS agent
ARG TOFU_UID=10001
ARG TOFU_GID=10001
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${TOFU_GID}" tofu \
    && useradd --uid "${TOFU_UID}" --gid "${TOFU_GID}" \
       --create-home --shell /usr/sbin/nologin tofu \
    && mkdir -p /app/logs /workspace /home/tofu/.config/tofu-agent \
    && chown -R tofu:tofu /app /workspace /home/tofu/.config
COPY --from=agent-builder --chown=tofu:tofu /opt/tofu-agent /opt/tofu-agent
WORKDIR /workspace
ENV PATH=/opt/tofu-agent/bin:$PATH \
    TOFU_AGENT_HOST=0.0.0.0 \
    TOFU_AGENT_PORT=15001 \
    TOFU_AGENT_CONFIG_PATH=/home/tofu/.config/tofu-agent/provider.json \
    MALLOC_ARENA_MAX=2 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TOFU_MANAGED_BY=docker
EXPOSE 15001
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://localhost:15001/health/live || exit 1
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["tofu-agent", "serve"]
USER tofu:tofu
