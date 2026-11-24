# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv-tool

# Apply Debian fixes to inherited packages before building development or production.
FROM debian:bookworm-slim@sha256:e67eeead736ff7bbe258ef088bff683635c561b1fcfefad0806c1df4ab49ce0e AS os-base
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# The interpreter comes from python-build-standalone because Debian's SQLite is older than
# the WAL-reset fix the database backend refuses to run without.
FROM os-base AS python-build
COPY --from=uv-tool /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PYTHON_DOWNLOADS=automatic
WORKDIR /tmp/dependencies
COPY pyproject.toml uv.lock ./
RUN uv python install 3.14 \
    && ln -s "$(ls -d /opt/uv-python/cpython-3.14-linux-* | head -1)" /opt/python \
    && uv export --locked --no-dev --no-emit-project --format requirements-txt \
       --output-file /tmp/requirements.txt \
    && uv export --locked --only-group dev --no-emit-project --format requirements-txt \
       --output-file /tmp/requirements-dev.txt \
    && /opt/python/bin/python3 -m pip install --no-cache-dir --require-hashes \
       --break-system-packages -r /tmp/requirements.txt

FROM os-base AS runtime-base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/python/bin:$PATH
COPY --from=python-build /opt/python /opt/python
COPY --from=python-build /tmp/requirements-dev.txt /opt/requirements-dev.txt
WORKDIR /app

FROM runtime-base AS development
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git config --system --add safe.directory /app \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv --system-site-packages /state/venv \
    && /state/venv/bin/python -m pip install --no-cache-dir --require-hashes --no-deps \
       -r /opt/requirements-dev.txt \
    && mv /state/venv /opt/venv-seed \
    && tar -C /opt/venv-seed -cf - . | sha256sum > /opt/venv-seed.sha256 \
    && useradd --uid 1000 --create-home developer \
    && mkdir -p /state/development /state/tmp && chown -R 1000:1000 /state
# No DJANGO_SETTINGS_MODULE here: manage.py defaults to development and pytest to test
# settings. An env var would override pytest's choice and point tests at the WAL database.
ENV PATH=/state/venv/bin:/opt/python/bin:$PATH \
    HEALTH_INFO_DATA_DIR=/state/development \
    HEALTH_INFO_TEST_DATA_DIR=/state/test-state \
    TMPDIR=/state/tmp \
    RUFF_CACHE_DIR=/state/ruff-cache \
    PYTEST_ADDOPTS="-o cache_dir=/state/pytest-cache" \
    PIP_REQUIRE_VIRTUALENV=true
COPY tools/dev_entrypoint.py /usr/local/bin/health-info-dev.py
COPY . /app
USER 1000:1000
ENTRYPOINT ["/opt/python/bin/python", "/usr/local/bin/health-info-dev.py"]
CMD ["sh", "-c", "python manage.py migrate --noinput && exec python manage.py runserver 0.0.0.0:8000"]

# A plain build produces the production image.
FROM runtime-base AS production
ARG HEALTH_INFO_VERSION=development
ENV HEALTH_INFO_VERSION=${HEALTH_INFO_VERSION} \
    DJANGO_SETTINGS_MODULE=health_info.settings.production \
    HEALTH_INFO_DATA_DIR=/data
RUN python -m pip uninstall --yes --break-system-packages pip \
    && mkdir -p /data && chown 10001:10001 /data
COPY manage.py /app/manage.py
COPY health_info /app/health_info
COPY records /app/records
COPY tools/__init__.py tools/check_production.py tools/healthcheck.py /app/tools/
COPY LICENSE NOTICE /app/
RUN HEALTH_INFO_SECRET_KEY=synthetic-build-key-never-for-runtime-000000000000 \
    HEALTH_INFO_ALLOWED_HOSTS=127.0.0.1 \
    HEALTH_INFO_CSRF_TRUSTED_ORIGINS=https://127.0.0.1 \
    HEALTH_INFO_DATA_DIR=/app/data \
    python manage.py collectstatic --noinput \
    && chown -R 10001:10001 /app/staticfiles
USER 10001:10001
RUN HEALTH_INFO_SECRET_KEY=synthetic-build-key-never-for-runtime-000000000000 \
    HEALTH_INFO_ALLOWED_HOSTS=127.0.0.1 \
    HEALTH_INFO_DATA_DIR=/app/data \
    python -c "from health_info.wsgi import application; assert application"
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "tools/healthcheck.py"]
CMD ["python", "-m", "health_info.server"]
