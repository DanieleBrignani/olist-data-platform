# Data quality strategy

Design: [ADR-0004](adr/0004-data-quality-layers-and-severity.md). This page covers the
implementation and the measured baselines on Olist v2.

## Severity semantics (one vocabulary for every layer)

| Severity | Record-level (raw → src) | Model-level (dbt tests) | Blocks publication? |
|----------|--------------------------|-------------------------|---------------------|
| **WARNING** | record kept in `src`, flagged in `meta.rejected_records` (`action = 'flagged'`) | failing rows stored and reported | never |
| **ERROR** | record quarantined: kept out of `src`, stored with rule, severity, run and reason (`action = 'quarantined'`) | blocks only when failing rows exceed the rule's `tolerance` | only beyond the tolerance; a file/table beyond its `max_reject_ratio` fails the run |
| **CRITICAL** | breaking schema change or checksum mismatch: the run stops before any write | blocks on **any** failing row; a test that cannot run also blocks (unverified = failed) | always |

Every dbt test declares `meta.dq_severity` (and `meta.tolerance` for ERROR). A test without a
classification is treated as CRITICAL: strict by default. dbt's own `severity`/`error_if`
mirror the classification, so `dbt test` on a laptop agrees with the gate. This is enforced
by `tests/unit/test_dq_classification.py`, which parses the dbt manifest.

## Where invalid data goes (nothing disappears silently)

| Layer | Stored in | Contents |
|-------|-----------|----------|
| raw (structure) | `meta.rejected_records` (`layer='raw'`) | original fields of records with the wrong field count or NUL bytes |
| src (contract rules) | `meta.rejected_records` (`layer='src'`) | quarantined and flagged records: rule, severity, reason, full raw record, file + row reference |
| warehouse (dbt tests) | `dq_failures.*` (all failing rows) + `meta.rejected_records` (`layer='warehouse'`, sample of ≤100 rows per failing test) + `meta.dq_results` | test, model, severity, failing row count, tolerance, blocking flag |
| decision | `meta.quality_gate_decisions` | PASS/FAIL per run, with the blocking reasons |

Each row carries `pipeline_run_id` and a timestamp.

## Rule coverage (the brief's list → where it is enforced)

| Rule | Layer / test | Severity | Olist v2 baseline |
|------|--------------|----------|-------------------|
| order_id uniqueness | src PK + `unique_fct_orders_order_id` + enforced PK | CRITICAL | 0 |
| customer relationship integrity | src FK (cascade quarantine) + `relationships` + enforced FK | ERROR (src) / CRITICAL (dbt) | 0 |
| product / seller relationship integrity | same | same | 0 |
| payment values ≥ 0 | contract `payment_value_non_negative` + `accepted_range` | ERROR | 0 |
| freight values ≥ 0 | contract `freight_non_negative` + `accepted_range` | ERROR | 0 |
| review_score in 1..5 | contract `review_score_in_range` + `accepted_values` | ERROR | 0 |
| purchase ≤ delivered | contract `delivered_not_before_purchase` + `timeline_is_ordered` | ERROR / CRITICAL | 0 |
| invalid future timestamps | contract `*_not_in_future` + generic `not_in_future` | ERROR / CRITICAL | 0 |
| missing mandatory identifiers | contract `not_null__*` + `pattern__*` + enforced NOT NULL | ERROR / CRITICAL | 0 |
| duplicate source records | staging `exact_duplicate` (flag + collapse) and `primary_key_conflict` (quarantine all versions) | WARNING / ERROR | 261,831 expected geolocation duplicates, collapsed |
| orphan records | staging FK checks against valid parents + dbt `relationships` | ERROR / CRITICAL | 0 ERROR orphans; WARNING existence gaps: 278 customer zips, 7 seller zips, 13 product categories |

## Custom tests (beyond unique / not_null / relationships / accepted_values)

| Test | Kind | Severity | Why it matters | Baseline |
|------|------|----------|----------------|----------|
| `assert_src_to_warehouse_reconciliation` | singular | CRITICAL | row counts per grain and money to the cent must match `src`; catches dropped, duplicated or rounded data | 0 mismatches |
| `assert_marts_reconcile_with_facts` | singular | CRITICAL | marts must add up to the facts, so reports and ad-hoc queries never disagree | 0 |
| `assert_shipping_limit_not_before_purchase` | singular, cross-file | CRITICAL | invisible to single-file contracts (the items file has no purchase date) | 0 |
| `timeline_is_ordered` | generic | CRITICAL (delivery, approval) / WARNING (carrier hand-off) | negative lead times corrupt delivery metrics | 0 / 166 |
| `not_in_future` | generic | CRITICAL | future events break time series and freshness | 0 |
| `accepted_range` | generic | ERROR | money must be non-negative, price positive | 0 |
| `assert_payments_cover_order_value` | singular | ERROR, tolerance 500 | payments short of the order value by more than R$1 (vouchers excluded; over-payment is consistent with instalment interest) | 18 of 94,454 eligible orders |
| `assert_monthly_order_volume_is_stable` | singular | WARNING | a month below 10% of its trailing 3-month average = incomplete period | 3 months: 2016-12, 2018-09, 2018-10 |
| `assert_reviews_not_answered_before_purchase` | singular, cross-file | WARNING | a review linked to the wrong order | 60 at date grain (63 at timestamp grain: 3 were answered on the purchase day, at an earlier hour) |
| `not_null` on `approved_at` where delivered | generic + `where` | WARNING | timeline gap | 14 |
| `accepted_range` on `payment_count` where not canceled | generic + `where` | WARNING | order without any payment record | 1 |

**How thresholds were chosen:** every baseline above was measured on Olist v2 *before* the
severity was assigned. A rule with real, explainable violations cannot be CRITICAL, or it
would block every publication; it becomes WARNING, or ERROR with a tolerance sized to the
observed noise. Rules that are 0 on real data *and* protect a metric are CRITICAL.

## The gate and publication

```
dbt_build ──► dbt_test (failures stored) ──► quality_gate ──PASS──► publish_marts (atomic swap)
                                                  │
                                                FAIL ──► run fails at quality_gate; the published
                                                         warehouse/marts are untouched
```

`publish_marts` also refuses to run unless `meta.quality_gate_decisions` holds PASS for the
same run (defence in depth). Rolling back to the previous publication is a metadata-only swap
(`olist rollback-publish`).

**Failure test** (`tests/integration/test_quality_gate_publish.py`): one order line whose
shipping deadline precedes its purchase. Ingestion and staging accept it, because it is valid
per every file contract. The CRITICAL cross-file rule catches it, the gate fails, the
published marts stay byte-for-byte as before, and the offending row is stored with rule,
severity, run id and timestamp.

## Great Expectations / Soda: evaluated, not adopted

ADR-0004 deferred this decision to Phase 7, with one test case: *independent reconciliation
checks that share no code with the transformations*. That need is now met by
`assert_src_to_warehouse_reconciliation`. It reads `src` directly and compares it with the
facts, so it shares no transformation logic, and its results flow through the same severity
model, quarantine table and gate. Adding GE or Soda would duplicate those checks in a second
framework with a second result format and another dependency set, and give no additional
guarantee. Per the rule "do not add technologies without a concrete architectural reason",
neither is used.
