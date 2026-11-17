#!/bin/sh
# PHONEINTEL PostgreSQL backup (POSIX sh; runs in the postgres:16-alpine "backup" compose profile).
#
#   docker compose --profile backup run --rm backup
#
# Produces a pg_dump custom-format archive. Restore with:
#   pg_restore --clean --if-exists --no-owner -d "$DATABASE" phoneintel_<timestamp>.dump
#
# Encrypted columns stay encrypted in the dump: keep FIELD_ENCRYPTION_KEYS and BLIND_INDEX_KEY
# backed up separately (a dump without its keys cannot be decrypted; keys without the dump are useless).
set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-phoneintel}"
POSTGRES_USER="${POSTGRES_USER:-phoneintel}"
TIMESTAMP="$(date -u +%Y%m%dT%H%M%SZ)"
TARGET="${BACKUP_DIR}/phoneintel_${TIMESTAMP}.dump"

umask 077
mkdir -p "${BACKUP_DIR}"

echo "backup: dumping ${POSTGRES_DB}@${POSTGRES_HOST} to ${TARGET}"
pg_dump -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  --format=custom --compress=9 --file="${TARGET}.partial"
# Verify the archive is readable before it replaces anything.
pg_restore --list "${TARGET}.partial" > /dev/null
mv "${TARGET}.partial" "${TARGET}"
echo "backup: done ($(du -h "${TARGET}" | cut -f1))"

find "${BACKUP_DIR}" -name 'phoneintel_*.dump' -type f -mtime "+${BACKUP_RETENTION_DAYS}" -exec rm -f {} \;
echo "backup: removed archives older than ${BACKUP_RETENTION_DAYS} days"
