# ── Stage 1: shared base with all common deps ──
FROM python:3.14-slim-bookworm AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Env
ENV TZ=UTC
ENV LANG=en_US.UTF-8
ENV PYTHON_JIT=1
ENV UV_COMPILE_BYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/usr/local
ENV UV_CACHE_DIR=/tmp/uv-cache

WORKDIR /app

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN --mount=type=cache,target=/tmp/uv-cache \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    uv sync --locked --no-install-project

RUN groupadd --gid 1000 pillbug \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/pillbug --shell /bin/bash pillbug \
    && install -d --owner 1000 --group 1000 /home/pillbug

ADD . .

RUN --mount=type=cache,target=/tmp/uv-cache \
    uv pip install --system -e .

RUN chmod +x run.sh docker-entrypoint.sh

# ── Stage 2: per-service layer ──
FROM base AS final

ARG PILLBUG_INSTALL_EXTRAS=""
ARG EXTRA_PACKAGES=""

RUN if [ -n "$EXTRA_PACKAGES" ]; then \
    apt-get update && apt-get install -y --no-install-recommends $EXTRA_PACKAGES && rm -rf /var/lib/apt/lists/*; \
    fi

RUN --mount=type=cache,target=/tmp/uv-cache \
    if [ -n "$PILLBUG_INSTALL_EXTRAS" ]; then \
    uv pip install --system -e ".[${PILLBUG_INSTALL_EXTRAS}]"; \
    fi

# Run
ENV HOME=/home/pillbug
ENTRYPOINT [ "/app/docker-entrypoint.sh" ]
CMD [ "/app/run.sh" ]
