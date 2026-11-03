"""Run the dbt project (plan 8.1) on the database of NEON_DATABASE_URL and report the result.

    python -m price_radar.transform.runner build [--select SELECTOR] [--full-refresh] [--summary FILE] [--annotate]
        [--notify always|problems|never]
    python -m price_radar.transform.runner docs

``build`` runs ``dbt build``: the seeds, models and tests in dependency order, a model's tests before the models that
read it. It exits 1 when a model, seed or test fails, so the nightly job fails and, with ``--notify problems``, Telegram
gets an alert. A test configured to warn (an offer whose device or condition label the reference tables lack) keeps the
exit code at 0 and is reported. ``docs`` writes the dbt documentation site to dbt/target.

The connection string is split into the variables dbt/profiles.yml reads. Host, user, password and database name go
into variables with dbt's ``DBT_ENV_SECRET_`` prefix, which dbt scrubs from everything it logs, and the messages this
module prints or sends are scrubbed of them too; the URL itself is never printed.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.conninfo import conninfo_to_dict

from price_radar.config import REPO_ROOT, database_url
from price_radar.notify import telegram

log = logging.getLogger(__name__)

PROJECT_DIR = REPO_ROOT / "dbt"
SECRET_VARIABLES = {
    "host": "DBT_ENV_SECRET_HOST",
    "user": "DBT_ENV_SECRET_USER",
    "password": "DBT_ENV_SECRET_PASSWORD",
    "dbname": "DBT_ENV_SECRET_DBNAME",
}
PROBLEMS = {"error", "fail", "warn", "skipped", "runtime error"}
FAILURES = {"error", "fail", "runtime error"}


class TransformError(RuntimeError):
    """dbt could not run at all (no project, no connection settings)."""


def connection_variables(url: str) -> dict[str, str]:
    """The environment dbt/profiles.yml expects, from a libpq connection string or URL."""
    params = conninfo_to_dict(url)
    missing = [key for key in ("host", "user", "dbname") if not params.get(key)]
    if missing:
        raise TransformError(f"the database URL has no {', '.join(missing)}")
    variables = {name: str(params[key]) for key, name in SECRET_VARIABLES.items() if params.get(key)}
    variables["DBT_PORT"] = str(params.get("port") or 5432)
    variables["DBT_SSLMODE"] = str(params.get("sslmode") or "require")
    return variables


def secrets_of(variables: Mapping[str, str]) -> list[str]:
    return [value for name, value in variables.items() if name.startswith("DBT_ENV_SECRET_") and value]


def scrub(text: str, secrets: Iterable[str]) -> str:
    """``text`` on one line, with every secret value replaced by ***."""
    for value in sorted(secrets, key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "***")
    return " ".join(text.split())


@dataclass(frozen=True)
class NodeResult:
    kind: str  # model, seed, test, ...
    name: str
    status: str  # success, pass, warn, fail, error, skipped
    message: str
    failures: int | None
    seconds: float


@dataclass(frozen=True)
class BuildReport:
    success: bool
    nodes: list[NodeResult]
    seconds: float
    error: str | None = None  # dbt failed before or outside the nodes (parsing, connection)

    @property
    def problems(self) -> list[NodeResult]:
        return [node for node in self.nodes if node.status in PROBLEMS]

    @property
    def failed(self) -> bool:
        return not self.success or self.error is not None or any(node.status in FAILURES for node in self.nodes)

    @property
    def warned(self) -> bool:
        return any(node.status == "warn" for node in self.nodes)


def node_results(result: Any, secrets: Iterable[str]) -> list[NodeResult]:
    """The nodes of a dbt run result (``dbtRunnerResult.result``)."""
    secrets = list(secrets)
    nodes = []
    for item in getattr(result, "results", None) or []:
        node = item.node
        nodes.append(
            NodeResult(
                kind=str(node.resource_type),
                name=node.name,
                status=str(item.status),
                message=scrub(str(item.message or ""), secrets),
                failures=item.failures,
                seconds=round(float(item.execution_time or 0), 1),
            )
        )
    return nodes


def counts_line(report: BuildReport) -> str:
    kinds = Counter(node.kind for node in report.nodes)
    statuses = Counter(node.status for node in report.nodes)
    built = ", ".join(f"{count} {kind}s" for kind, count in sorted(kinds.items())) or "nothing"
    outcome = ", ".join(f"{count} {status}" for status, count in sorted(statuses.items())) or "no results"
    return f"dbt build: {built}; {outcome} ({report.seconds:.0f} s)"


def problem_line(node: NodeResult) -> str:
    detail = f" ({node.failures} rows)" if node.kind == "test" and node.failures else ""
    message = f": {node.message}" if node.message and node.status != "skipped" else ""
    return f"{node.status} {node.kind} {node.name}{detail}{message}"


def report_lines(report: BuildReport) -> list[str]:
    lines = [counts_line(report)]
    if report.error:
        lines.append(f"error: {report.error}")
    skipped = [node for node in report.problems if node.status == "skipped"]
    lines += [problem_line(node) for node in report.problems if node.status != "skipped"]
    if skipped:
        lines.append(f"skipped after a failure: {', '.join(node.name for node in skipped)}")
    return lines


def markdown(report: BuildReport) -> str:
    lines = report_lines(report)
    return "### dbt build\n\n" + "\n".join(f"- {line}" for line in lines) + "\n"


def annotations(report: BuildReport) -> list[str]:
    level = "error" if report.failed else "warning" if report.warned else "notice"
    head, *rest = report_lines(report)
    lines = [f"::{level} title=dbt build::{head}"]
    for line in rest[:9]:  # GitHub shows at most 10 annotations of a kind per step
        lines.append(f"::{'warning' if line.startswith('warn') else level} title=dbt::{line}")
    return lines


def status_word(report: BuildReport) -> str:
    return "ALERT" if report.failed else "WARNING" if report.warned else "OK"


def run_dbt(args: list[str], variables: Mapping[str, str]) -> Any:
    """Invoke dbt in this process with the connection variables set. Returns the dbtRunnerResult."""
    from dbt.cli.main import dbtRunner  # imported here: dbt is heavy and only this command needs it

    if not (PROJECT_DIR / "dbt_project.yml").exists():
        raise TransformError(f"no dbt project in {PROJECT_DIR}")
    os.environ.update(variables)
    command = [*args, "--project-dir", str(PROJECT_DIR), "--profiles-dir", str(PROJECT_DIR)]
    return dbtRunner().invoke(command)


def build(
    variables: Mapping[str, str], *, select: str | None = None, full_refresh: bool = False, extra: Iterable[str] = ()
) -> BuildReport:
    args = ["build"]
    if select:
        args += ["--select", select]
    if full_refresh:
        args.append("--full-refresh")
    args += list(extra)
    started = time.monotonic()
    secrets = secrets_of(variables)
    result = run_dbt(args, variables)
    error = scrub(f"{type(result.exception).__name__}: {result.exception}", secrets) if result.exception else None
    return BuildReport(
        success=bool(result.success),
        nodes=node_results(result.result, secrets),
        seconds=time.monotonic() - started,
        error=error,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m price_radar.transform.runner")
    commands = parser.add_subparsers(dest="command", required=True)
    build_cmd = commands.add_parser("build", help="dbt build: seeds, models and tests")
    build_cmd.add_argument("--select", help="a dbt node selection, e.g. 'marts' or 'fct_offer_daily+'")
    build_cmd.add_argument(
        "--full-refresh",
        action="store_true",
        help="rebuild incremental models; the marts refuse it (full_refresh false)",
    )
    build_cmd.add_argument("--summary", type=Path, help="append a Markdown summary to this file (the job summary)")
    build_cmd.add_argument("--annotate", action="store_true", help="print GitHub Actions annotations")
    build_cmd.add_argument(
        "--notify",
        choices=["always", "problems", "never"],
        default="problems",
        help="send a Telegram message always, only on failures and warnings, or never",
    )
    commands.add_parser("docs", help="write the documentation site to dbt/target")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")

    url = database_url()
    if not url:
        print("NEON_DATABASE_URL is not set (environment or .env)", file=sys.stderr)
        return 2
    try:
        variables = connection_variables(url)
    except (TransformError, ValueError, psycopg.Error) as err:
        # The parser's message may quote the URL: only the error type is shown.
        message = str(err) if isinstance(err, TransformError) else type(err).__name__
        print(f"cannot read NEON_DATABASE_URL: {message}", file=sys.stderr)
        return 2
    secrets = [url, *secrets_of(variables)]

    if args.command == "docs":
        result = run_dbt(["docs", "generate"], variables)
        print(f"dbt docs generate: {'done' if result.success else 'failed'}; site in {PROJECT_DIR / 'target'}")
        return 0 if result.success else 1

    try:
        report = build(variables, select=args.select, full_refresh=args.full_refresh)
    except Exception as err:  # reported and turned into the exit code
        report = BuildReport(success=False, nodes=[], seconds=0.0, error=scrub(f"{type(err).__name__}: {err}", secrets))
    lines = report_lines(report)
    print("\n".join(lines))
    if args.summary:
        with args.summary.open("a", encoding="utf-8") as f:
            f.write(markdown(report))
    if args.annotate:
        print("\n".join(annotations(report)))
    if args.notify == "always" or (args.notify == "problems" and (report.failed or report.warned)):
        sent = telegram.send("\n".join([f"[{status_word(report)}] iPhone Price Radar, dbt build", *lines]))
        log.info("Telegram message %s", "sent" if sent else "not sent")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
