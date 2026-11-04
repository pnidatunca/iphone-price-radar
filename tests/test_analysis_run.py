from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from price_radar.analysis import run
from price_radar.analysis.run import AnalysisError, Table, cell, check_columns, dbt_vars, markdown, table_markdown

URL = "postgresql://owner_user:s3cret-pass@ep-example-123.eu-central-1.aws.neon.tech/neondb?sslmode=require"


def test_the_period_becomes_dbt_vars():
    assert dbt_vars() == {}
    assert dbt_vars(date(2026, 11, 14), 7, analysis_min_offers=1) == {
        "analysis_end": "2026-11-14",
        "analysis_days": 7,
        "analysis_min_offers": 1,
    }
    with pytest.raises(ValueError, match="at least 1"):
        dbt_vars(days=0)


def test_columns_that_identify_sellers_or_offers_are_refused():
    check_columns("a3_seller_map", ["seller_rank", "mean_position_pct"])
    with pytest.raises(AnalysisError, match="seller_key"):
        check_columns("a3_seller_map", ["seller_key", "mean_position_pct"])


def test_cells_are_plain_text():
    assert [cell(v) for v in (None, True, Decimal("49.8644"), Decimal("1E+2"), date(2026, 11, 4), "a|b", 0.123456)] == [
        "",
        "yes",
        "49.8644",
        "100",
        "2026-11-04",
        "a/b",
        "0.1235",
    ]


def test_long_tables_are_cut_in_the_page_but_not_in_the_csv(tmp_path):
    long = Table(
        "a3_seller_map",
        "One row per seller.",
        ["seller_rank", "mean_position_pct"],
        [(n, Decimal("-1.5")) for n in range(30)],
    )
    empty = Table("a1_fit", "", ["models"], [])
    page = table_markdown(long, max_rows=25)
    assert page.startswith(
        "### A3 · Anonymous seller map\n\nOne row per seller.\n\n| seller_rank | mean_position_pct |"
    )
    assert "| 24 | -1.5 |" in page and "| 25 | -1.5 |" not in page
    assert "_5 more rows in a3_seller_map.csv._" in page
    assert "_No rows._" in table_markdown(empty)

    text = markdown([long, empty], generated=datetime(2026, 11, 4, 15, 0, tzinfo=UTC))
    assert text.startswith("## Analyses\n\nComputed 2026-11-04 15:00 UTC")
    paths = run.write([long, empty], tmp_path / "out", text)
    assert [p.name for p in paths] == ["analyses.md", "a3_seller_map.csv", "a1_fit.csv"]
    rows = (tmp_path / "out" / "a3_seller_map.csv").read_text(encoding="utf-8").splitlines()
    assert rows[0] == "seller_rank,mean_position_pct" and len(rows) == 31


def test_main_needs_a_valid_url(monkeypatch, capsys):
    monkeypatch.setattr(run, "database_url", lambda: None)
    assert run.main([]) == 2
    monkeypatch.setattr(run, "database_url", lambda: "host=localhost dbname=radar user='unclosed")
    assert run.main([]) == 2
    monkeypatch.setattr(run, "database_url", lambda: URL)
    assert run.main(["--days", "0"]) == 2
    assert "unclosed" not in capsys.readouterr().err


def test_a_failure_is_reported_without_the_connection_details(monkeypatch, capsys):
    def broken(variables, values):
        raise RuntimeError(f"could not connect to {variables['DBT_ENV_SECRET_HOST']} as owner_user")

    monkeypatch.setattr(run, "database_url", lambda: URL)
    monkeypatch.setattr(run, "compile_analyses", broken)
    assert run.main([]) == 1
    err = capsys.readouterr().err
    assert "analyses failed: RuntimeError: could not connect to *** as ***" in err
    assert "neon.tech" not in err and "owner_user" not in err


def test_main_writes_the_tables_and_the_summary(monkeypatch, tmp_path, capsys):
    seen = {}

    def compiled(variables, values):
        seen["values"] = values
        return [run.Analysis("overview", "Headline figures.", "select 1")]

    monkeypatch.setattr(run, "database_url", lambda: URL)
    monkeypatch.setattr(run, "compile_analyses", compiled)
    monkeypatch.setattr(
        run, "run_analyses", lambda url, analyses: [Table(a.name, a.description, ["days"], [(21,)]) for a in analyses]
    )
    summary = tmp_path / "summary.md"
    assert run.main(["--end", "2026-11-14", "--out", str(tmp_path / "out"), "--summary", str(summary)]) == 0
    assert seen["values"] == {"analysis_end": "2026-11-14"}
    assert "| 21 |" in (tmp_path / "out" / "analyses.md").read_text(encoding="utf-8")
    assert summary.read_text(encoding="utf-8").startswith("## Analyses")
    out = capsys.readouterr().out
    assert "overview: 1 rows" in out and "s3cret" not in out
