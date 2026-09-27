#!/usr/bin/env bash
# Runs once, on first initialisation of the data volume, as the superuser.
# Creates least-privilege roles and databases (docs/adr/0009-least-privilege-roles.md).
# Schema-level grants are owned by Alembic migrations, not by this script.
#
# Two warehouse databases with identical security: olist_dw (real data) and olist_dw_test
# (integration tests truncate tables freely there without touching real data).
set -euo pipefail

: "${OLIST_ADMIN_PASSWORD:?missing}" "${OLIST_PIPELINE_PASSWORD:?missing}"
: "${OLIST_REPORTING_PASSWORD:?missing}" "${OLIST_MONITOR_PASSWORD:?missing}"
: "${PREFECT_DB_PASSWORD:?missing}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  -v admin_pw="$OLIST_ADMIN_PASSWORD" \
  -v pipeline_pw="$OLIST_PIPELINE_PASSWORD" \
  -v reporting_pw="$OLIST_REPORTING_PASSWORD" \
  -v monitor_pw="$OLIST_MONITOR_PASSWORD" \
  -v prefect_pw="$PREFECT_DB_PASSWORD" <<'SQL'
CREATE ROLE olist_admin     LOGIN PASSWORD :'admin_pw';
CREATE ROLE olist_pipeline  LOGIN PASSWORD :'pipeline_pw';
CREATE ROLE olist_reporting LOGIN PASSWORD :'reporting_pw';
CREATE ROLE olist_monitor   LOGIN PASSWORD :'monitor_pw';
CREATE ROLE prefect         LOGIN PASSWORD :'prefect_pw';

-- Reporting and monitoring sessions must never hold locks long enough to block publication
ALTER ROLE olist_reporting SET statement_timeout = '60s';
ALTER ROLE olist_monitor   SET statement_timeout = '10s';

CREATE DATABASE prefect OWNER prefect;
REVOKE ALL ON DATABASE prefect FROM PUBLIC;
SQL

for db in olist_dw olist_dw_test; do
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres -v db="$db" <<'SQL'
CREATE DATABASE :"db" OWNER olist_admin;
REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO olist_pipeline, olist_reporting, olist_monitor;
-- dbt creates its own schemas (stg, int, *_build) and the publish step renames them
GRANT CREATE ON DATABASE :"db" TO olist_pipeline;
SQL
  # Lock down the public schema: nobody but the owner creates objects there
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$db" <<'SQL'
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO olist_admin;
SQL
done
