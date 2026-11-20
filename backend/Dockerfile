# syntax=docker/dockerfile:1
# ---- base: runtime dependencies ------------------------------------------------
FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN groupadd --system app && useradd --system --gid app --home /app app \
    && mkdir -p /data/uploads && chown -R app:app /data
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- dev: adds test/lint tooling; source is bind-mounted by docker compose -----
FROM base AS dev
ENV UPLOAD_DIR=/data/uploads
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY --chown=app:app . .
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- prod: minimal image, non-root ----------------------------------------------
FROM base AS prod
# FORWARDED_ALLOW_IPS: comma-separated proxy IPs/CIDRs whose X-Forwarded-For/-Proto uvicorn
# trusts (uvicorn reads this env var itself, so the CMD below must NOT pass
# --forwarded-allow-ips). Override it at deploy time with your load balancer's addresses;
# the app refuses to start in staging/production if it is "*".
ENV UPLOAD_DIR=/data/uploads \
    FORWARDED_ALLOW_IPS=127.0.0.1
COPY --chown=app:app alembic.ini ./
COPY --chown=app:app alembic ./alembic
COPY --chown=app:app app ./app
USER app
EXPOSE 8000
# Run migrations as a separate release step (see docs/DEPLOYMENT.md), not on every boot.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "2"]
