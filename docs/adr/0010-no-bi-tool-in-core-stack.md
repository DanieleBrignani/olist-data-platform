# ADR-0010: No BI tool in the core stack

**Status:** Accepted (2026-09-26)

## Context
The brief makes Metabase and Superset optional. The Docker host has 8 GB of RAM, and Metabase
alone needs about 1 GB for its JVM. This repository's audience is evaluating data engineering,
not dashboard design.

## Decision
The consumption contract is the `marts` schema plus the `olist_reporting` role.
`scripts/business_report.py` produces the business metrics reproducibly: it queries the marts
as `olist_reporting` and writes `docs/business_metrics.md`. Any BI tool can connect with the
reporting credentials, but none is shipped.

## Consequences
+ A smaller footprint, a faster `docker compose up`, and fewer moving parts to break in CI.
− There is no clickable business dashboard. Grafana covers operational metrics only.
