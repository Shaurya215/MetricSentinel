# One image, two services: the API runs uvicorn, the UI runs streamlit.
# Only the core dependency group is installed; `ds` and `agent` are added in the
# phases that need them.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

# Dependency layer: cached until pyproject.toml or uv.lock changes. The uv
# download cache lives in a BuildKit cache mount, so it speeds up rebuilds
# without being baked into the image.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

COPY . .

# Run as a non-root user. Code and venv stay root-owned and read-only to it:
# the app writes nothing into the image (data arrives via a mounted volume),
# and a `chown -R` here would duplicate the whole venv into a new layer.
RUN useradd --create-home --uid 10001 sentinel
USER sentinel

EXPOSE 8000
CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
