#!/usr/bin/env bash
#
# TimeHub backup — logical Postgres dump -> restic (HDD) -> restic copy (S3).
#
# Pipeline (see docs/backup.md):
#   1. pg_dump (custom format, consistent, no downtime) of the timehub DB
#   2. restic backup of that dump into the local repo on the HDD
#   3. restic copy of the new snapshot to the off-site Hetzner S3 repo
#   4. GFS pruning (keep-daily/weekly/monthly/yearly) on both repos
#
# Uploads (timehub_uploads) are intentionally NOT backed up — the uploaded
# CSVs only matter once, at import time.
#
# Config is read from an env file (default /etc/timehub/backup.env). See
# scripts/backup.env.example for all variables.

set -euo pipefail

# --- Load config ------------------------------------------------------------
CONFIG_FILE="${TIMEHUB_BACKUP_CONFIG:-/etc/timehub/backup.env}"
if [[ -f "$CONFIG_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  set +a
fi

# --- Defaults ---------------------------------------------------------------
DEPLOY_DIR="${DEPLOY_DIR:-/opt/timehub}"
COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.prod.yml}"
ENV_FILE="${ENV_FILE:-stack.env}"
DB_SERVICE="${DB_SERVICE:-db}"
PGUSER="${PGUSER:-timehub}"
PGDATABASE="${PGDATABASE:-timehub}"

BACKUP_ROOT="${BACKUP_ROOT:-/mnt/backup-hdd}"
LOCAL_REPO="${LOCAL_REPO:-${BACKUP_ROOT}/restic-repo}"
STAGING_DIR="${STAGING_DIR:-${BACKUP_ROOT}/staging}"

# Whether to include stack.env (secrets) in the backup so a restore is
# self-contained. Off by default: the secrets already live in GitHub secrets
# and are re-rendered into stack.env on deploy. Set to true to make the backup
# fully self-contained (restic encrypts the repo, so it is safe either way).
INCLUDE_ENV_FILE="${INCLUDE_ENV_FILE:-false}"

# restic auth (same password used for both repos)
RESTIC_PASSWORD_FILE="${RESTIC_PASSWORD_FILE:-/etc/timehub/restic-password}"
export RESTIC_PASSWORD_FILE

# Off-site copy to Hetzner S3
ENABLE_S3="${ENABLE_S3:-true}"
S3_REPO="${S3_REPO:-}"   # e.g. s3:https://fsn1.your-objectstorage.com/<bucket>/timehub

# GFS retention — local HDD (shorter, it is the fast-restore tier)
KEEP_DAILY="${KEEP_DAILY:-7}"
KEEP_WEEKLY="${KEEP_WEEKLY:-4}"
KEEP_MONTHLY="${KEEP_MONTHLY:-6}"
KEEP_YEARLY="${KEEP_YEARLY:-0}"

# GFS retention — off-site S3 (longer, the archive tier)
S3_KEEP_DAILY="${S3_KEEP_DAILY:-14}"
S3_KEEP_WEEKLY="${S3_KEEP_WEEKLY:-8}"
S3_KEEP_MONTHLY="${S3_KEEP_MONTHLY:-12}"
S3_KEEP_YEARLY="${S3_KEEP_YEARLY:-3}"

# --- Helpers ----------------------------------------------------------------
log() { printf '[backup] %s %s\n' "$(date -u +%FT%TZ)" "$*"; }
die() { printf '[backup] ERROR: %s\n' "$*" >&2; exit 1; }

compose() {
  docker compose -f "${DEPLOY_DIR}/${COMPOSE_FILE}" --env-file "${DEPLOY_DIR}/${ENV_FILE}" "$@"
}

restic_local() { restic -r "$LOCAL_REPO" "$@"; }

ensure_repo() {
  # $1 = repo
  if ! restic -r "$1" cat config >/dev/null 2>&1; then
    log "initialising restic repo: $1"
    restic -r "$1" init
  fi
}

# --- Preflight --------------------------------------------------------------
command -v docker >/dev/null || die "docker not found"
command -v restic >/dev/null || die "restic not found"
[[ -f "$RESTIC_PASSWORD_FILE" ]] || die "restic password file missing: $RESTIC_PASSWORD_FILE"
[[ -d "$BACKUP_ROOT" ]] || die "backup root not mounted: $BACKUP_ROOT"
[[ -f "${DEPLOY_DIR}/${COMPOSE_FILE}" ]] || die "compose file missing: ${DEPLOY_DIR}/${COMPOSE_FILE}"

TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WORKDIR="${STAGING_DIR}/${TIMESTAMP}"
mkdir -p "$WORKDIR"
# Always clean the staging dir, even on failure.
trap 'rm -rf "$WORKDIR"' EXIT

# --- 1. Dump the database ---------------------------------------------------
DUMP_FILE="${WORKDIR}/timehub-${TIMESTAMP}.dump"
log "dumping database '${PGDATABASE}' (custom format)..."
# -Fc = custom format: compressed, consistent, restorable with pg_restore and
# portable across Postgres versions. -T runs without a TTY.
compose exec -T "$DB_SERVICE" pg_dump -U "$PGUSER" -Fc "$PGDATABASE" > "$DUMP_FILE"
[[ -s "$DUMP_FILE" ]] || die "dump is empty — aborting before it overwrites good backups"
log "dump written: $(du -h "$DUMP_FILE" | cut -f1)"

# Include the secrets file so a restore can bring the stack back up as-is.
if [[ "$INCLUDE_ENV_FILE" == "true" && -f "${DEPLOY_DIR}/${ENV_FILE}" ]]; then
  cp "${DEPLOY_DIR}/${ENV_FILE}" "${WORKDIR}/${ENV_FILE}"
  log "included ${ENV_FILE} in backup set"
fi

# --- 2. Back up to the local HDD repo --------------------------------------
ensure_repo "$LOCAL_REPO"
log "backing up to local repo: $LOCAL_REPO"
restic_local backup --tag timehub --tag db --host timehub "$WORKDIR"

# --- 3. Copy off-site to Hetzner S3 ----------------------------------------
if [[ "$ENABLE_S3" == "true" ]]; then
  [[ -n "$S3_REPO" ]] || die "ENABLE_S3=true but S3_REPO is not set"
  [[ -n "${AWS_ACCESS_KEY_ID:-}" && -n "${AWS_SECRET_ACCESS_KEY:-}" ]] \
    || die "ENABLE_S3=true but AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY are not set"
  ensure_repo "$S3_REPO"
  log "copying snapshots to off-site repo: $S3_REPO"
  # copy reads from the local repo (FROM) into the S3 repo. Same password.
  RESTIC_FROM_PASSWORD_FILE="$RESTIC_PASSWORD_FILE" \
    restic -r "$S3_REPO" copy --from-repo "$LOCAL_REPO"
fi

# --- 4. GFS pruning ---------------------------------------------------------
log "pruning local repo (GFS)..."
restic_local forget --prune \
  --keep-daily   "$KEEP_DAILY" \
  --keep-weekly  "$KEEP_WEEKLY" \
  --keep-monthly "$KEEP_MONTHLY" \
  --keep-yearly  "$KEEP_YEARLY"

if [[ "$ENABLE_S3" == "true" ]]; then
  log "pruning off-site repo (GFS)..."
  restic -r "$S3_REPO" forget --prune \
    --keep-daily   "$S3_KEEP_DAILY" \
    --keep-weekly  "$S3_KEEP_WEEKLY" \
    --keep-monthly "$S3_KEEP_MONTHLY" \
    --keep-yearly  "$S3_KEEP_YEARLY"
fi

log "backup completed successfully."
