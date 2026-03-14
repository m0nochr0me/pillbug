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

# Install
WORKDIR /app

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project

ADD . .

RUN uv pip install --system -e .

# Run
RUN chmod +x run.sh
ENTRYPOINT [ "/app/run.sh" ]
