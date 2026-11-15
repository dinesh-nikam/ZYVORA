#!/usr/bin/env bash
# PHONEINTEL Database Backup Script
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
TIMESTAMP=$(date -u +"%Y%m%d_%H%M%SZ")
POSTGRES_HOST="${POSTGRES_HOST:-localhost}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_DB="${POSTGRES_DB:-phoneintel}"
POSTGRES_USER="${POSTGRES_USER:-phoneintel}"

mkdir -p "${BACKUP_DIR}"

BACKUP_FILE="${BACKUP_DIR}/phoneintel_backup_${TIMESTAMP}.sql.gz"

echo "[*] Starting backup of database '${POSTGRES_DB}' at ${TIMESTAMP}..."

PGPASSWORD="${POSTGRES_PASSWORD:-}" pg_dump -h "${POSTGRES_HOST}" -p "${POSTGRES_PORT}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  --format=custom --compress=9 --file="${BACKUP_FILE}"

echo "[+] Backup successfully created: ${BACKUP_FILE}"
echo "[*] Size: $(du -h "${BACKUP_FILE}" | cut -f1)"

# Keep last 14 days of backups
find "${BACKUP_DIR}" -name "phoneintel_backup_*.sql.gz" -mtime +14 -exec rm {} \;
echo "[+] Retention check completed. Older backups removed."
