"""`olist` command-line entrypoint. Later phases add ingest / run / benchmark commands."""

from __future__ import annotations

import time
from pathlib import Path

import typer
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from olist_platform.config import Role, get_settings
from olist_platform.database.engine import get_engine
from olist_platform.database.locking import pipeline_lock
from olist_platform.database.publish import rollback
from olist_platform.errors import DataContractError, PlatformError
from olist_platform.ingestion.manifest import (
    MANIFEST_NAME,
    build_manifest,
    write_lock,
    write_manifest,
)
from olist_platform.ingestion.pipeline import run_ingestion, run_staging
from olist_platform.ingestion.runs import tracked_run
from olist_platform.ingestion.source import KAGGLE_DATASET, locate_or_download, raw_dir
from olist_platform.transform.dbt_runner import run_dbt
from olist_platform.transform.warehouse import run_warehouse
from olist_platform.utils.logging import configure_logging, get_logger
from olist_platform.validation.contracts import load_contracts
from olist_platform.validation.docs import render as render_contract_docs
from olist_platform.validation.schema import check_file, enforce

app = typer.Typer(no_args_is_help=True, add_completion=False)
db_app = typer.Typer(no_args_is_help=True, help="Database migrations.")
contracts_app = typer.Typer(no_args_is_help=True, help="Source data contracts.")
source_app = typer.Typer(no_args_is_help=True, help="Raw source files.")
app.add_typer(db_app, name="db")
app.add_typer(contracts_app, name="contracts")
app.add_typer(source_app, name="source")

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"


@app.callback()
def _setup() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.env, settings.log_file)


@app.command()
def doctor() -> None:
    """Check that every application role can connect. Exit code 1 on any failure."""
    log = get_logger("doctor")
    failed = False
    for role in (Role.ADMIN, Role.PIPELINE, Role.REPORTING, Role.MONITOR):
        started = time.perf_counter()
        try:
            with get_engine(role).connect() as conn:
                user = conn.execute(text("select current_user")).scalar_one()
            log.info(
                "db_connect",
                role=role.value,
                current_user=user,
                status="ok",
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
        except Exception as exc:
            failed = True
            log.error(
                "db_connect",
                role=role.value,
                status="failed",
                error_type=type(exc).__name__,
                error=str(exc).splitlines()[0],
            )
    raise typer.Exit(code=1 if failed else 0)


DERIVED_SCHEMAS = (
    "stg",
    "int",
    "warehouse_build",
    "marts_build",
    "warehouse",
    "marts",
    "warehouse_prev",
    "marts_prev",
    "dq_failures",
)


@db_app.command("drop-derived")
def db_drop_derived(yes: bool = typer.Option(False, "--yes", help="Confirm")) -> None:
    """Drop the dbt-built schemas (fully rebuildable). Required before downgrading past
    migration 0003, whose src tables the dbt views depend on."""
    if not yes:
        typer.echo("refusing without --yes: this drops published warehouse/marts schemas")
        raise typer.Exit(code=1)
    try:
        with pipeline_lock("drop_derived"), get_engine(Role.PIPELINE).begin() as conn:
            for schema in DERIVED_SCHEMAS:  # the pipeline role owns the dbt schemas
                conn.execute(text(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    except PlatformError as exc:
        get_logger("migrations").error(
            "drop_derived_failed", error_type=exc.error_type, error=str(exc)
        )
        raise typer.Exit(code=1) from exc
    get_logger("migrations").info("derived_schemas_dropped", schemas=list(DERIVED_SCHEMAS))


@db_app.command("upgrade")
def db_upgrade(revision: str = "head") -> None:
    """Apply Alembic migrations as the admin role."""
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    command.upgrade(cfg, revision)
    get_logger("migrations").info("db_upgrade", revision=revision, status="ok")


@contracts_app.command("validate")
def contracts_validate() -> None:
    """Load every contract and check internal + cross-contract consistency."""
    contracts = load_contracts()
    get_logger("contracts").info(
        "contracts_valid",
        tables=sorted(contracts),
        rules=sum(len(c.rules) for c in contracts.values()),
        status="ok",
    )


@contracts_app.command("docs")
def contracts_docs(out: Path = PROJECT_ROOT / "docs" / "data_contracts.md") -> None:
    """Regenerate docs/data_contracts.md from the YAML contracts."""
    out.write_text(render_contract_docs(load_contracts()), encoding="utf-8")
    get_logger("contracts").info("contracts_docs_written", path=str(out), status="ok")


@contracts_app.command("check-source")
def contracts_check_source(data_root: Path = DEFAULT_DATA_ROOT) -> None:
    """Validate every source file's encoding and header against its contract.

    Exit code 1 if any file has a breaking change; non-breaking changes are only logged.
    """
    log = get_logger("contracts")
    directory = raw_dir(data_root)
    failed = []
    for contract in load_contracts().values():
        path = directory / contract.file
        if not path.is_file():
            log.error("source_missing", source=contract.file, status="failed")
            failed.append(contract.file)
            continue
        result = check_file(path, contract)
        try:
            enforce(result)
            log.info(
                "schema_check",
                source=contract.file,
                table=contract.table,
                status="ok",
                non_breaking=len(result.non_breaking),
            )
        except DataContractError:
            failed.append(contract.file)
    raise typer.Exit(code=1 if failed else 0)


@source_app.command("manifest")
def source_manifest(
    data_root: Path = DEFAULT_DATA_ROOT,
    write_lock_file: bool = typer.Option(
        False, "--write-lock", help="Also (re)write data_contracts/manifest.lock.json"
    ),
) -> None:
    """Compute size/sha256/row_count of every raw file; write manifest.json once."""
    contracts = load_contracts()
    directory = raw_dir(data_root)
    entries = build_manifest(directory, contracts)
    log = get_logger("source")
    if (directory / MANIFEST_NAME).exists():
        log.info("manifest_exists", path=str(directory / MANIFEST_NAME), action="kept")
    else:
        log.info("manifest_written", path=str(write_manifest(directory, entries)))
    if write_lock_file:
        version = next(iter(contracts.values())).dataset_version
        log.info("lock_written", path=str(write_lock(entries, KAGGLE_DATASET, version)))
    for e in entries:
        log.info(
            "manifest_entry", source=e.filename, size=e.size, sha256=e.sha256, row_count=e.row_count
        )


@app.command()
def ingest(
    data_root: Path = DEFAULT_DATA_ROOT,
    force_reload: bool = typer.Option(False, help="Reload files already loaded (same sha256)"),
) -> None:
    """verify_manifest -> validate_source -> ingest_raw into the raw schema."""
    settings = get_settings()
    try:
        summary = run_ingestion(
            get_engine(Role.PIPELINE),
            raw_dir(data_root),
            load_contracts(),
            environment=settings.env,
            force_reload=force_reload,
        )
    except PlatformError as exc:
        get_logger("ingest").error("ingest_failed", error_type=exc.error_type, error=str(exc))
        raise typer.Exit(code=1) from exc
    for r in summary.results:
        typer.echo(
            f"{r.status:8} {r.table:30} read={r.rows_read:>9,} "
            f"loaded={r.rows_loaded:>9,} rejected={r.rows_rejected:>5,} "
            f"{r.duration_ms:>9,.0f} ms"
        )


@app.command()
def stage() -> None:
    """load_staging: rebuild typed src tables from the latest raw files (one transaction)."""
    try:
        results = run_staging(
            get_engine(Role.PIPELINE), load_contracts(), environment=get_settings().env
        )
    except PlatformError as exc:
        get_logger("stage").error("stage_failed", error_type=exc.error_type, error=str(exc))
        raise typer.Exit(code=1) from exc
    for r in results:
        typer.echo(
            f"{r.table:30} raw={r.raw_rows:>9,} src={r.src_rows:>9,} "
            f"quarantined={r.quarantined:>5,} flagged={r.flagged:>5,} "
            f"collapsed={r.duplicates_collapsed:>7,}"
        )


@app.command()
def transform(
    select: str = typer.Option(None, help="dbt selector, e.g. marts.core+"),
) -> None:
    """dbt build: staging/intermediate views, warehouse_build and marts_build tables + tests."""
    args = ["build"] + (["--select", select] if select else [])
    try:
        with pipeline_lock("olist_transform"):  # writes the dbt build schemas
            run = run_dbt(args)
    except PlatformError as exc:
        get_logger("transform").error("transform_failed", error_type=exc.error_type, error=str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"dbt build ok in {run.duration_ms:,.0f} ms: {run.by_status()}")


@app.command()
def warehouse(
    publish_marts: bool = typer.Option(True, "--publish/--no-publish"),
) -> None:
    """dbt_build -> dbt_test -> quality_gate -> publish_marts (atomic schema swap)."""
    contracts = load_contracts()
    try:
        summary = run_warehouse(
            get_engine(Role.PIPELINE),
            environment=get_settings().env,
            dataset_version=next(iter(contracts.values())).dataset_version,
            publish_marts=publish_marts,
        )
    except PlatformError as exc:
        get_logger("warehouse").error(
            "warehouse_failed", error_type=exc.error_type, error=str(exc)[:500]
        )
        raise typer.Exit(code=1) from exc
    d = summary.decision
    typer.echo(f"quality gate PASS: {d.evaluated} tests, {len(d.warnings)} warnings")
    for w in d.warnings:
        typer.echo(f"  WARNING {w.test_name}: {w.failures} rows")
    if summary.published_rows is not None:
        typer.echo(
            f"published {len(summary.published_rows)} tables, "
            f"{sum(summary.published_rows.values()):,} rows"
        )


@app.command("rollback-publish")
def rollback_publish() -> None:
    """Swap the published warehouse/marts with the previous version (*_prev)."""
    engine = get_engine(Role.PIPELINE)
    version = next(iter(load_contracts().values())).dataset_version
    try:
        with tracked_run(engine, "olist_rollback", get_settings().env, version) as run:
            run.step("rollback")
            counts = rollback(engine, run.pipeline_run_id)
    except PlatformError as exc:
        get_logger("rollback").error("rollback_failed", error_type=exc.error_type, error=str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(f"rolled back: {len(counts)} tables now published from the previous version")


@app.command("run")
def run_flow(
    data_root: Path = DEFAULT_DATA_ROOT,
    force_reload: bool = typer.Option(False, help="Reload files already loaded"),
    publish_enabled: bool = typer.Option(True, "--publish/--no-publish"),
    full_refresh: bool = typer.Option(
        False, "--full-refresh", help="Rebuild even when inputs are unchanged"
    ),
) -> None:
    """Run the full olist_refresh flow once (against PREFECT_API_URL, or ephemeral)."""
    from orchestration.olist_flow import olist_refresh  # noqa: PLC0415 - heavy import

    try:
        summary = olist_refresh(
            str(data_root), force_reload, publish_enabled, full_refresh=full_refresh
        )
    except PlatformError as exc:
        get_logger("run").error("flow_failed", error_type=exc.error_type, error=str(exc)[:500])
        raise typer.Exit(code=1) from exc
    typer.echo(f"olist_refresh succeeded: {summary}")


@app.command()
def exporter(port: int = typer.Option(9108, help="HTTP port for /metrics")) -> None:
    """Serve Prometheus metrics derived from the metadata tables (read-only monitor role)."""
    from olist_platform.observability.exporter import serve  # noqa: PLC0415

    serve(get_engine(Role.MONITOR), port)


@app.command("fingerprint")
def fingerprint_cmd() -> None:
    """Print row count + content md5 of every published table (compare runs with diff)."""
    from olist_platform.database.fingerprint import fingerprint  # noqa: PLC0415

    with get_engine(Role.REPORTING).connect() as conn:
        for fp in fingerprint(conn).values():
            typer.echo(f"{fp.table:45} {fp.rows:>9,}  {fp.md5}")


@source_app.command("fetch")
def source_fetch(data_root: Path = DEFAULT_DATA_ROOT) -> None:
    """Locate the pinned Olist files locally or download them from Kaggle (read-only)."""
    files = [c.file for c in load_contracts().values()]
    location = locate_or_download(data_root, files)
    get_logger("source").info(
        "source_ready", action=location.action, directory=str(location.directory), status="ok"
    )


if __name__ == "__main__":
    app()
