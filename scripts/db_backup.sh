#!/usr/bin/env bash
# Logical backup of one database (default: olist_dw) with pg_dump custom format.
# Runs pg_dump INSIDE the postgres container: no client tools needed on the host.
# Usage: scripts/db_backup.sh [database]   -> prints the path of the written dump
set -euo pipefail
DB="${1:-olist_dw}"
OUT_DIR="${BACKUP_DIR:-backups}"
COMPOSE="${COMPOSE:-docker compose}"
mkdir -p "$OUT_DIR"
OUT="$OUT_DIR/${DB}_$(date -u +%Y%m%dT%H%M%SZ).dump"
$COMPOSE exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" --format=custom "$1"' _ "$DB" > "$OUT"
# A dump that cannot be listed is not a backup: verify before reporting success
$COMPOSE exec -T postgres pg_restore --list < "$OUT" > /dev/null
echo "$OUT"
