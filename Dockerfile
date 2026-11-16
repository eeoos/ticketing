# Ticketing API image (app1~3 share this image; see docs/infra.md)
FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend ./backend
RUN useradd --system --uid 10001 --no-create-home app
USER app
ENV PATH="/app/.venv/bin:$PATH" WORKERS=2
EXPOSE 8000
HEALTHCHECK --interval=2s --timeout=3s --retries=15 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/shows', timeout=2).status == 200 else 1)"
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS}"]
