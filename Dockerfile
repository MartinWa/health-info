# One non-root Waitress process; the container filesystem stays read-only.
FROM python:3.14-alpine

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=health_info.settings.development

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .
RUN python manage.py collectstatic --noinput && rm -rf .data && chown -R 10001:10001 /app

ENV DJANGO_SETTINGS_MODULE=health_info.settings.production

RUN addgroup -S -g 10001 app && adduser -S -D -H -u 10001 -G app app
USER 10001

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
  CMD ["python", "tools/healthcheck.py"]

CMD ["python", "-m", "health_info.server"]
