#!/usr/bin/env bash
# Restore a pg_dump custom-format backup into an EXISTING database (default: olist_dw),
# replacing its objects, in ONE transaction: a failed restore leaves the database unchanged.
# Usage: scripts/db_restore.sh <dump-file> [database]
set -euo pipefail
FILE="${1:?usage: scripts/db_restore.sh <dump-file> [database]}"
DB="${2:-olist_dw}"
COMPOSE="${COMPOSE:-docker compose}"
test -s "$FILE" || { echo "no such backup: $FILE" >&2; exit 1; }
$COMPOSE exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$1" --clean --if-exists --single-transaction --exit-on-error' \
  _ "$DB" < "$FILE"
echo "restored $FILE into $DB"
