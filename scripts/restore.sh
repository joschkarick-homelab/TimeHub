#!/usr/bin/env bash
#
# TimeHub restore — pull a snapshot from restic and load it into Postgres.
#
# DESTRUCTIVE: this drops and recreates the objects in the target database.
# The app is stopped during the restore and started again afterwards.
#
# Usage:
#   scripts/restore.sh [--from local|s3] [--snapshot <id>] [--yes]
#
#   --from local|s3   which repo to restore from (default: local)
#   --snapshot <id>   restic snapshot id (default: latest)
#   --yes             skip the confirmation prompt
#
# Config is read from the same env file as backup.sh.

set -euo pipefail

# --- Load config ------------------------------------------------------------
CONFIG_FILE="${TIMEHUB_BACKUP_CONFIG:-/etc/timehub/backup.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  set +a
fi

DEPLOY_DIR="${DEPLOY_DIR:-/opt/timehub}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-stack.env}"
DB_SERVICE="${DB_SERVICE:-db}"
APP_SERVICE="${APP_SERVICE:-app}"
PGUSER="${PGUSER:-timehub}"
PGDATABASE="${PGDATABASE:-timehub}"

BACKUP_ROOT="${BACKUP_ROOT:-/mnt/backup-hdd}"
LOCAL_REPO="${LOCAL_REPO:-${BACKUP_ROOT}/restic-repo}"
S3_REPO="${S3_REPO:-}"
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/etc/timehub/restic-password}"
export RESTIC_PASSWORD_FILE

# --- Args -------------------------------------------------------------------
FROM="local"
SNAPSHOT="latest"
ASSUME_YES="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)     FROM="$2"; shift 2 ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --yes|-y)   ASSUME_YES="true"; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

case "$FROM" in
  local) REPO="$LOCAL_REPO" ;;
  s3)    REPO="$S3_REPO"; [[ -n "$REPO" ]] || { echo "S3_REPO not set" >&2; exit 1; } ;;
  *)     echo "--from must be 'local' or 's3'" >&2; exit 1 ;;
esac

log() { printf '[restore] %s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { printf '[restore] ERROR: %s\n' "$*" >&2; exit 1; }
compose() {
  docker compose -f "${DEPLOY_DIR}/${COMPOSE_FILE}" --env-file "${DEPLOY_DIR}/${ENV_FILE}" "$@"
}

command -v restic >/dev/null || die "restic not found"
[[ -f "$RESTIC_PASSWORD_FILE" ]] || die "restic password file missing: $RESTIC_PASSWORD_FILE"

# --- Confirm ----------------------------------------------------------------
log "restoring from repo: $REPO (snapshot: $SNAPSHOT)"
if [[ "$ASSUME_YES" != "true" ]]; then
  read -r -p "This will OVERWRITE the '${PGDATABASE}' database. Type 'yes' to continue: " ans
  [[ "$ans" == "yes" ]] || die "aborted by user"
fi

# --- Fetch the snapshot -----------------------------------------------------
RESTORE_TMP="$(mktemp -d)"
trap 'rm -rf "$RESTORE_TMP"' EXIT
log "fetching snapshot into $RESTORE_TMP ..."
restic -r "$REPO" restore "$SNAPSHOT" --target "$RESTORE_TMP"

DUMP_FILE="$(find "$RESTORE_TMP" -name '*.dump' -type f | sort | tail -n1)"
[[ -n "$DUMP_FILE" ]] || die "no .dump file found in snapshot"
log "found dump: $DUMP_FILE"

# --- Restore ----------------------------------------------------------------
log "stopping app to release DB connections..."
compose stop "$APP_SERVICE" || true

log "loading dump into '${PGDATABASE}' (pg_restore --clean --if-exists)..."
# --clean --if-exists drops existing objects before recreating them, so the DB
# ends up matching the dump exactly. --no-owner keeps it portable.
compose exec -T "$DB_SERVICE" \
  pg_restore -U "$PGUSER" -d "$PGDATABASE" --clean --if-exists --no-owner < "$DUMP_FILE"

log "starting app..."
compose up -d "$APP_SERVICE"

log "restore completed. Verify the app at /healthz and spot-check the data."
