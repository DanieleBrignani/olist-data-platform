# Continuous integration

There is CI but no CD: nothing is built into a registry or deployed (see the last section).

Workflow: [`.github/workflows/ci.yml`](../.github/workflows/ci.yml). It runs on pushes to
`main`, on pull requests and on demand. Every step fails the workflow on a non-zero exit code.

| Job | Checks | Fails when |
|-----|--------|------------|
| **lint** (no DB) | ruff lint, ruff format check (locked versions via `uv sync --frozen`), `promtool check config` on the Prometheus config and alert rules, `actionlint` on the workflow itself | any lint/format finding, invalid PromQL or rule, invalid workflow |
| **test** | build images; start Postgres; apply migrations; **schema checks**: migrations upgrade → downgrade to base → upgrade on a fresh database, `olist contracts validate`; **dbt compile**; **pytest** via `make test-ci` (unit + integration + e2e on synthetic data, with coverage + JUnit report); the **dbt tests** run inside those integration tests against the real dbt project; **backup/restore round-trip** (`make verify-backup DB=olist_dw_test`) | any failing test, migration that cannot be reversed, invalid contract, dbt compile error, a restore that does not reproduce the database |
| **real-data** (after test) | restores the Olist v2 files from cache (key = hash of the committed checksum lock) or downloads them; verifies them against the lock; runs `ingest → stage → warehouse` on the full dataset, which includes **dbt test + quality gate**; then the real-data e2e and idempotency tests (3 full pipeline runs) | checksum mismatch, breaking schema change, **any CRITICAL data-quality failure** (the gate exits 1), non-identical reruns |
| **images** | builds the runtime, dev and Grafana images | a Dockerfile no longer builds |

**A critical failure fails the pipeline.** A CRITICAL data-quality result makes
`olist warehouse` exit 1, which fails the `real-data` job, and therefore the workflow.
Deterministic failures are never retried; they are surfaced.

## Verification status

| Level | What it means | Status |
|-------|---------------|--------|
| Locally validated | the same commands on the Windows development host (Docker Desktop) | all jobs' commands passed before the first push |
| Simulated CI | the `test` job on a clean export of the committed files, in a separate compose project with a fresh database, as a non-default uid, with GNU make in a Linux container (`docker:28-cli`) | passed before the first push; `actionlint` passes |
| **GitHub Actions** | the workflow on GitHub-hosted `ubuntu-24.04` runners | first passing run on 2026-10-01 (record below). Current runs are listed under the repository's Actions tab; the README badge shows the latest status |

### First runs on GitHub (2026-10-01)

A historical record: these runs took place in the original GitHub repository, which was
later recreated from a rewritten history, so they are no longer listed under Actions and the
commit identifiers differ ([mapping](production-readiness-review.md#commit-identifiers)).

| Run | Commit | Result | Notes |
|-----|--------|--------|-------|
| CI #1 | first push | lint **failed**, then cancelled | `astral-sh/setup-uv@v10` could not be resolved: since v8 that action publishes only full version tags (`v10.2.0`), no floating major tag. The local simulation could not catch it, because it does not resolve actions |
| CI #2 | history rewrite | cancelled | superseded by the fix (`concurrency: cancel-in-progress`) |
| CI #3 | `18f6d5a` | **success** in 22 min 33 s | lint 28 s, images 1 min 54 s, tests 12 min 1 s, real data 9 min 39 s (anonymous Kaggle download worked; the dataset is now cached by lock hash) |

Run #3 also showed that the runner has enough memory and disk for the real-data job, and that
the anonymous Kaggle download works from GitHub. If Kaggle starts requiring authentication,
the cache entry keyed by the checksum lock still covers later runs.

## Secrets

CI needs no repository secrets. `scripts/make_env.py` creates a fresh `.env` from
`.env.example` with random URL-safe passwords on every run, and it refuses to overwrite an
existing `.env`. The dataset comes from Kaggle's public API and needs no credentials.

## Running the CI test job locally

The `test` job was run end to end on a clean export of the committed files, in a separate
compose project with a fresh database, as a non-default uid (the Linux runner case):

```bash
git checkout-index -a -f --prefix=/tmp/olist-ci/ && cd /tmp/olist-ci
python scripts/make_env.py
DEV_UID=1001 DEV_GID=118 docker compose -p olistci --profile dev build migrate dev
docker compose -p olistci up -d --wait postgres && docker compose -p olistci up --exit-code-from migrate migrate
DEV_UID=1001 DEV_GID=118 docker compose -p olistci run --rm dev pytest -m "not source_data"
```

## Why the dev container user is configurable

The repository is bind-mounted into the dev container. On Linux the container must run as the
uid that owns the checkout, or it cannot write `dbt/target`, `logs/` or `data/`. Docker Desktop
on Windows/macOS hides this, so it was caught by simulating the runner, not by local runs.
`DEV_UID`/`DEV_GID` default to the image user (10001) locally; CI sets them from `id -u`/`id -g`.

## Not included (deliberately)

* **Deployment / image publishing (CD):** there is no registry or target environment to deploy
  to. The `images` job only checks that the images build.
* **Scheduled real-data runs:** the dataset is a static snapshot; `workflow_dispatch` allows an
  on-demand rerun.
