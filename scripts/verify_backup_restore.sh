#!/usr/bin/env bash
# Backup/restore round-trip proof: fingerprint every table of meta/raw/src/warehouse/marts,
# back up, DAMAGE the database, restore, fingerprint again - the two must be identical.
# Usage: scripts/verify_backup_restore.sh [database]   (used by `make verify-backup` and CI)
set -euo pipefail
DB="${1:-olist_dw}"
COMPOSE="${COMPOSE:-docker compose}"
HERE="$(cd "$(dirname "$0")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

psql_db() { $COMPOSE exec -T postgres sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1" -At' _ "$DB"; }

fingerprint() {
  psql_db <<'SQL'
SELECT format('SELECT %L, count(*), md5(coalesce(string_agg(t::text, ''|'' ORDER BY t::text), '''')) FROM %I.%I t',
              schemaname || '.' || tablename, schemaname, tablename)
FROM pg_tables WHERE schemaname IN ('meta', 'raw', 'src', 'warehouse', 'marts') ORDER BY 1
\gexec
SQL
}

fingerprint > "$TMP/before"
test -s "$TMP/before" || { echo "nothing to verify in $DB" >&2; exit 1; }
DUMP="$(BACKUP_DIR="$TMP" "$HERE/db_backup.sh" "$DB")"
echo "backup: $(du -h "$DUMP" | cut -f1) for $(wc -l < "$TMP/before") tables"

# Damage: lose a published schema and a raw table's contents
psql_db <<'SQL' > /dev/null
SET client_min_messages = warning;
DROP SCHEMA IF EXISTS marts CASCADE;
TRUNCATE raw.orders;
SQL
fingerprint > "$TMP/damaged"
if diff -q "$TMP/before" "$TMP/damaged" > /dev/null; then echo "damage step had no effect" >&2; exit 1; fi

"$HERE/db_restore.sh" "$DUMP" "$DB" > /dev/null
fingerprint > "$TMP/after"
if diff "$TMP/before" "$TMP/after"; then
  echo "backup/restore round-trip OK: $(wc -l < "$TMP/after") tables identical in $DB"
else
  echo "backup/restore round-trip FAILED: content differs" >&2; exit 1
fi
