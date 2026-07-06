#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-.venv/bin/python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
fi

docker_compose() {
  docker compose -f docker-compose.yml -f docker-compose.prod.yml "$@"
}

echo "==> Python syntax check"
"$PYTHON_BIN" -m compileall -q app scripts healthcheck.py

echo "==> Repository safety check"
"$PYTHON_BIN" scripts/safety_check.py

echo "==> Unit tests"
"$PYTHON_BIN" -m pytest

echo "==> Docker Compose production config"
docker compose version >/dev/null
docker_compose config --quiet

if [ "${SKIP_DOCKER_BUILD:-0}" = "1" ]; then
  echo "==> Docker build skipped because SKIP_DOCKER_BUILD=1"
elif ! docker info >/dev/null 2>&1; then
  echo "==> Docker build skipped because Docker daemon is unavailable"
else
  echo "==> Docker build"
  docker_compose build
fi

echo "Pre-deploy checks passed"
