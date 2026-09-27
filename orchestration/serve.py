"""Long-running worker: registers the `olist-refresh` deployment with the Prefect server and
executes its runs (triggered from the UI, `prefect deployment run`, or an optional cron).

    OLIST_REFRESH_CRON="0 3 * * *"   # optional schedule; unset = on demand only
"""

from __future__ import annotations

import os

from orchestration.olist_flow import olist_refresh


def main() -> None:
    cron = os.environ.get("OLIST_REFRESH_CRON") or None
    olist_refresh.serve(
        name="olist-refresh",
        cron=cron,
        tags=["olist", "batch"],
        description="Raw Olist files -> quality-gated, published warehouse and marts.",
        limit=1,  # never two refreshes at once: they would race for the schema swap
    )


if __name__ == "__main__":
    main()
