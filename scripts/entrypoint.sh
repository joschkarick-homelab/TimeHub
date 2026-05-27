#!/usr/bin/env bash
set -euo pipefail

echo "[entrypoint] running alembic migrations..."
alembic upgrade head

echo "[entrypoint] starting: $*"
exec "$@"
