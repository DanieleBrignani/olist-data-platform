# ADR-0004: Layered data quality with WARNING / ERROR / CRITICAL severities

**Status:** Accepted (2026-09-26). The Great Expectations / Soda question deferred below was decided in Phase 7: neither was adopted; see [data_quality.md](../data_quality.md#great-expectations--soda-evaluated-not-adopted).

## Context
Quality problems appear at different layers and need different responses:
* a missing column must stop everything;
* an orphan order item must not reach a fact table, but it must not vanish either;
* a review with an unusually long comment is only worth noting.

The brief requires that invalid records never disappear silently and that critical failures
block publication.

## Decision
There are three enforcement points and one severity vocabulary.

| Layer | Tool | Examples | Outcome |
|-------|------|----------|---------|
| File / schema | contract engine (`src/olist_platform/validation`, YAML contracts in `data_contracts/`) | missing or renamed column, wrong column count, checksum mismatch | breaking change → CRITICAL, run fails before any DB write; non-breaking change (extra column) → WARNING, logged |
| Record (`raw` → `src`) | set-based SQL generated from the contracts | timestamp that cannot be cast, NULL mandatory id, negative price, duplicate key, orphan FK | row written to `meta.rejected_records` (record reference, rule, severity, run id, timestamp, reason); only valid rows enter `src` |
| Model (dbt) | dbt generic and custom tests, `severity` + `meta.dq_severity` | uniqueness, relationships, accepted values, delivered ≥ purchased, no future timestamps, src ↔ fact reconciliation | results parsed from `run_results.json` into `meta.dq_results` |

**What each severity means**
* **WARNING**: recorded and exposed as a metric. Publication continues.
* **ERROR**: the offending records are quarantined or excluded. Publication continues only if
  the failure rate stays under the rule's threshold, which each contract rule defines.
* **CRITICAL**: the quality gate fails the run. The build schemas are *not* swapped in, so the
  previously published warehouse stays untouched (ADR-0005).

The quality gate is a pure function, `(dq_results, thresholds) → PASS | FAIL(reasons)`, and is
unit-tested without a database.

**Great Expectations / Soda:** the brief offers these as options. Whether a third framework adds
anything beyond the contract engine and dbt tests is decided in Phase 7. The test case will be
one concrete job: independent reconciliation checks that share no code with the
transformations. If the framework adds no independent guarantee, it is left out and the reason
is documented, following the "no technology without a reason" rule.

## Consequences
+ Every rejected record can be queried together with its original text, through `raw` lineage.
+ One severity model across all layers gives one Grafana panel and one gate.
− The contract engine is custom code and needs thorough tests (Phase 10).
