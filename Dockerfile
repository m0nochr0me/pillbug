# Base + uv
FROM python:3.14-slim-bookworm
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Env
ENV TZ=UTC
ENV LANG=en_US.UTF-8
ENV UV_NO_CACHE=1
ENV PYTHON_JIT=1
ENV UV_COMPILE_BYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV UV_PROJECT_ENVIRONMENT=/usr/local
ENV HOME=/home/pillbug

# Install
WORKDIR /app

ARG PILLBUG_INSTALL_EXTRAS=""

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=packages,target=packages \
    uv sync --locked --no-install-project

RUN groupadd --gid 1000 pillbug \
    && useradd --uid 1000 --gid 1000 --create-home --home-dir /home/pillbug --shell /bin/bash pillbug \
    && install -d --owner 1000 --group 1000 /var/lib/pillbug \
    && install -d --owner 1000 --group 1000 /var/lib/pillbug-dashboard

ADD . .

RUN if [ -n "$PILLBUG_INSTALL_EXTRAS" ]; then \
    uv pip install --system -e ".[${PILLBUG_INSTALL_EXTRAS}]"; \
    else \
    uv pip install --system -e .; \
    fi

# Run
RUN chmod +x run.sh docker-entrypoint.sh
ENTRYPOINT [ "/app/docker-entrypoint.sh" ]
CMD [ "/app/run.sh" ]
