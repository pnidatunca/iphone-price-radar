"""Run the analyses of dbt/analyses on the database of NEON_DATABASE_URL and write their tables.

    python -m price_radar.analysis.run [--end YYYY-MM-DD] [--days N] [--out DIR] [--summary FILE]

The analyses (plan 10.1: A1-A3 and A6, with the headline figures of a market report) read the marts only, so they
can be run again for any period the marts hold. dbt compiles them and never runs them; each compiled query then runs
here in one read-only transaction. DIR gets one CSV per analysis and all of them in analyses.md; ``--summary``
appends that page to a file (the job summary). The tables hold aggregates only: a column that identifies a seller or
an offer is refused before anything is written (sellers are ranked instead, K22). The connection string is never
printed, and errors are scrubbed of it.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg

from price_radar.config import database_url
from price_radar.transform import runner
from price_radar.transform.runner import TransformError

DEFAULT_OUT = runner.PROJECT_DIR / "target" / "analyses"
ORDER = [
    "overview",
    "a1_age_curve",
    "a1_fit",
    "a1_storage_steps",
    "a2_conditions",
    "a3_spread",
    "a3_seller_positions",
    "a3_seller_map",
    "a6_supply_weekly",
    "a6_supply_by_generation",
    "a6_supply_by_model",
]
TITLES = {
    "overview": "Overview",
    "a1_age_curve": "A1 · Depreciation by generation",
    "a1_fit": "A1 · Depreciation per year of age",
    "a1_storage_steps": "A1 · What more storage is worth",
    "a2_conditions": "A2 · Condition differences",
    "a3_spread": "A3 · Price spread within a segment",
    "a3_seller_positions": "A3 · Seller positions",
    "a3_seller_map": "A3 · Anonymous seller map",
    "a6_supply_weekly": "A6 · Supply by week",
    "a6_supply_by_generation": "A6 · Supply by generation",
    "a6_supply_by_model": "A6 · Supply by model",
}
# Columns that would identify a seller or an offer: an analysis returning one is refused.
IDENTIFYING = {"seller_key", "seller_id", "offer_key", "offer_url", "variant_id", "product_url"}
MARKDOWN_ROWS = 25
STATEMENT_TIMEOUT_MS = 120_000


class AnalysisError(RuntimeError):
    """The analyses could not be compiled or run, or one returned what must not leave the database."""


@dataclass(frozen=True)
class Analysis:
    name: str
    description: str
    sql: str


@dataclass(frozen=True)
class Table:
    name: str
    description: str
    columns: list[str]
    rows: list[tuple]


def dbt_vars(end: date | None = None, days: int | None = None, **extra: Any) -> dict[str, Any]:
    values: dict[str, Any] = dict(extra)
    if end is not None:
        values["analysis_end"] = end.isoformat()
    if days is not None:
        if days < 1:
            raise ValueError("--days must be at least 1")
        values["analysis_days"] = days
    return values


def order_key(name: str) -> tuple[int, str]:
    return (ORDER.index(name) if name in ORDER else len(ORDER), name)


def compile_analyses(variables: Mapping[str, str], values: Mapping[str, Any] | None = None) -> list[Analysis]:
    """The analyses compiled by dbt for the database of ``variables``, in report order."""
    args = ["compile", "--select", "resource_type:analysis", "--quiet"]
    if values:
        args += ["--vars", json.dumps(dict(values))]
    result = runner.run_dbt(args, variables)
    if not result.success:
        detail = f"{type(result.exception).__name__}: {result.exception}" if result.exception else "see the dbt log"
        raise AnalysisError(runner.scrub(f"dbt compile failed: {detail}", runner.secrets_of(variables)))
    analyses = [
        Analysis(item.node.name, " ".join((item.node.description or "").split()), item.node.compiled_code)
        for item in result.result.results
        if str(item.node.resource_type) == "analysis"
    ]
    if not analyses:
        raise AnalysisError("dbt compiled no analyses")
    return sorted(analyses, key=lambda a: order_key(a.name))


def check_columns(name: str, columns: Iterable[str]) -> None:
    if found := sorted(IDENTIFYING & set(columns)):
        raise AnalysisError(f"{name} returns {', '.join(found)}, which identifies sellers or offers")


def run_analyses(url: str, analyses: Iterable[Analysis]) -> list[Table]:
    """Each analysis in one read-only transaction, so none can change the database."""
    tables = []
    with psycopg.connect(url) as conn:
        conn.read_only = True
        conn.execute(f"set statement_timeout = {STATEMENT_TIMEOUT_MS}")
        for analysis in analyses:
            cur = conn.execute(analysis.sql)
            columns = [column.name for column in cur.description or []]
            check_columns(analysis.name, columns)
            tables.append(Table(analysis.name, analysis.description, columns, cur.fetchall()))
    return tables


def cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return f"{value:.4g}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value).replace("|", "/")


def table_markdown(table: Table, max_rows: int = MARKDOWN_ROWS) -> str:
    lines = [f"### {TITLES.get(table.name, table.name)}", ""]
    if table.description:
        lines += [table.description, ""]
    if not table.rows:
        return "\n".join([*lines, "_No rows._", ""])
    lines.append("| " + " | ".join(table.columns) + " |")
    lines.append("|" + "---|" * len(table.columns))
    lines += ["| " + " | ".join(cell(v) for v in row) + " |" for row in table.rows[:max_rows]]
    if len(table.rows) > max_rows:
        lines += ["", f"_{len(table.rows) - max_rows} more rows in {table.name}.csv._"]
    return "\n".join([*lines, ""])


def markdown(tables: Iterable[Table], *, generated: datetime | None = None) -> str:
    generated = generated or datetime.now(UTC)
    head = [
        "## Analyses",
        "",
        f"Computed {generated:%Y-%m-%d %H:%M} UTC from the marts (dbt/analyses, methods in docs/analyses.md).",
        "",
    ]
    return "\n".join(head) + "\n".join(table_markdown(table) for table in tables)


def write(tables: list[Table], out: Path, page: str) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths = []
    for table in tables:
        path = out / f"{table.name}.csv"
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(table.columns)
            writer.writerows([cell(v) for v in row] for row in table.rows)
        paths.append(path)
    page_path = out / "analyses.md"
    page_path.write_text(page, encoding="utf-8")
    return [page_path, *paths]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.analysis.run")
    parser.add_argument("--end", type=date.fromisoformat, help="last day of the period (default: the latest day built)")
    parser.add_argument("--days", type=int, help="days of the cross-sectional window (default: var analysis_days)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"output directory (default: {DEFAULT_OUT})")
    parser.add_argument("--summary", type=Path, help="append the Markdown page to this file (the job summary)")
    args = parser.parse_args(argv)

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    try:
        variables = runner.connection_variables(url)
        values = dbt_vars(args.end, args.days)
    except (TransformError, ValueError, psycopg.Error) as err:
        message = str(err) if isinstance(err, TransformError | ValueError) and "://" not in str(err) else ""
        print(f"cannot run the analyses: {message or type(err).__name__}", file=sys.stderr)
        return 2
    secrets = [url, *runner.secrets_of(variables)]
    try:
        tables = run_analyses(url, compile_analyses(variables, values))
    except Exception as err:  # reported without the connection details
        print(f"analyses failed: {runner.scrub(f'{type(err).__name__}: {err}', secrets)}", file=sys.stderr)
        return 1
    page = markdown(tables)
    paths = write(tables, args.out, page)
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(page)
    for table in tables:
        print(f"{table.name}: {len(table.rows)} rows")
    print(f"wrote {len(paths)} files to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
