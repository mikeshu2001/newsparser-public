# ---- Build stage ----
FROM python:3.11-slim AS builder

WORKDIR /build

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---- Runtime stage ----
FROM python:3.11-slim

# Non-root user
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Codex CLI for the optional Codex provider (CODEX_PROVIDER_ENABLED=true).
# The npm package ships the platform binary. No credentials are baked into
# the image: auth.json is mounted at runtime into /app/.codex.
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm ca-certificates \
    && npm install -g @openai/codex \
    && rm -rf /var/lib/apt/lists/* /root/.npm

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY app/ app/
COPY prompts/ prompts/
COPY scripts/migrations/ scripts/migrations/
COPY healthcheck.py .

# Own by non-root user (/tmp writable by default)
RUN chown -R appuser:appuser /app

USER appuser

# Healthcheck — verify the scheduler ran recently
HEALTHCHECK --interval=60s --timeout=15s --start-period=300s --retries=3 \
    CMD python healthcheck.py

CMD ["python", "-m", "app.main"]
