# Security

Scope: a local, single-node platform on public data. The goals are that no credential
ever enters git, that every component has the least privilege it needs, and that known
vulnerabilities in dependencies are checked. Audit date: 2026-09-29.

## Audit results

| Check | Tool / method | Result |
|-------|---------------|--------|
| Secrets in the working tree and **all git history** | gitleaks 8.28.0 (`gitleaks git`) | no leaks found |
| Real `.env` values anywhere in git | every password/secret value of the local `.env` searched with `git log --all -S` and `git grep` | 0 of 7 found |
| Sensitive files tracked | `git ls-files` for `.env`, keys, certificates, credentials | none; `.env` is git-ignored and was never added |
| Python dependency vulnerabilities | pip-audit 2.9.0 on the 147 packages pinned in `uv.lock` (all groups) | no known vulnerabilities |
| Runtime image vulnerabilities | Trivy 0.67.2, HIGH/CRITICAL with a fix available, OS packages (Debian 12.15) + Python packages + binaries | 0 |
| Insecure code patterns | ruff `S` (flake8-bandit) rules in CI, 4 justified `noqa` | clean |

Reproduce:

```bash
docker run --rm -v "$PWD:/repo" zricethezav/gitleaks:v8.28.0 git /repo --redact
docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.67.2 image --scanners vuln --ignore-unfixed --severity HIGH,CRITICAL olist-platform:runtime
```

pip-audit ran inside the dev image on `uv export --frozen --no-hashes --all-groups`. These
scans are one-off, not part of CI: see "Not covered".

## Controls in place

* **Secrets.** `scripts/make_env.py` generates `.env` from `.env.example` with random
  URL-safe passwords, one per role, and refuses to overwrite an existing `.env`.
  `.env.example` holds only `CHANGE_ME` placeholders. CI generates a fresh `.env` per run and
  needs no repository secrets. The Kaggle download is anonymous.
* **Least privilege** (ADR-0009): five roles.
  * `olist_admin` owns the base schemas.
  * `olist_pipeline` writes data and performs the swap.
  * `olist_reporting` can only `SELECT` the published `warehouse`/`marts`.
  * `olist_monitor` can only read `meta`.
  * `prefect` owns only its own database.

  `PUBLIC` loses every default privilege. Demoted `*_prev` schemas lose reporting access at
  the swap. Integration tests assert these boundaries (`tests/integration/test_database_roles.py`).
* **Network exposure.** Every published port is bound to `127.0.0.1`.
* **Containers.** Application images run as a non-root user (`olist`, uid 10001). Log
  rotation is bounded.
* **SQL.** Identifiers interpolated into SQL come only from contracts, whose table names are
  validated against `^[a-z][a-z0-9_]*$`. Values are always bound parameters.

## Not covered (known gaps)

* Prefect has no authentication, and Grafana allows anonymous **viewer** access. This is
  acceptable only because both are bound to localhost; a shared deployment needs SSO or
  authentication.
* Dependency and image scans are not scheduled in CI. A production setup would add
  pip-audit/Trivy jobs and Dependabot.
* No TLS between containers (one Docker network on one host).
* Secrets are a local file, not a secrets manager.
