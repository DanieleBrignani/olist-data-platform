# CI/CD

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
| Locally validated | the same commands, run on the Windows development host (Docker Desktop) | all jobs' commands pass |
| Simulated CI | the `test` job run on a clean export of the committed files, in a separate compose project with a fresh database, as a non-default uid, with GNU make inside a Linux container (`docker:28-cli`); `actionlint` passes | passes |
| **GitHub Actions** | the workflow run on GitHub-hosted `ubuntu-24.04` runners | **not yet run**: the repository has not been pushed. No "CI passing" claim is made anywhere |

Things that can only fail on GitHub: action major versions resolving differently, runner disk
or memory limits during the real-data job (about 1.5 M rows plus dbt, 734 MB database), and
the anonymous Kaggle download if Kaggle starts requiring authentication (after the first
successful run, the `actions/cache` entry keyed by the checksum lock removes that dependency).

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

* **Deployment / image publishing:** there is no registry or target environment, so a CD step
  would be fake infrastructure. The `images` job proves the deployable artefacts build.
* **Scheduled real-data runs:** the dataset is a static snapshot; `workflow_dispatch` allows an
  on-demand rerun.
