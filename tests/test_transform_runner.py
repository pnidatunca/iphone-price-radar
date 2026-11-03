from types import SimpleNamespace

import pytest

from price_radar.transform import runner
from price_radar.transform.runner import (
    BuildReport,
    NodeResult,
    TransformError,
    annotations,
    connection_variables,
    markdown,
    node_results,
    report_lines,
    scrub,
    status_word,
)

URL = "postgresql://owner_user:s3cret-pass@ep-example-123.eu-central-1.aws.neon.tech/neondb?sslmode=require"


def node(kind="model", name="fct_offer_daily", status="success", message="SELECT 10", failures=None):
    return NodeResult(kind, name, status, message, failures, 1.0)


def test_the_url_is_split_into_the_profile_variables():
    variables = connection_variables(URL)
    assert variables == {
        "DBT_ENV_SECRET_HOST": "ep-example-123.eu-central-1.aws.neon.tech",
        "DBT_ENV_SECRET_USER": "owner_user",
        "DBT_ENV_SECRET_PASSWORD": "s3cret-pass",
        "DBT_ENV_SECRET_DBNAME": "neondb",
        "DBT_PORT": "5432",
        "DBT_SSLMODE": "require",
    }
    local = connection_variables("postgresql://postgres@localhost:55432/radar?sslmode=disable")
    assert "DBT_ENV_SECRET_PASSWORD" not in local  # profiles.yml defaults to an empty password
    assert (local["DBT_PORT"], local["DBT_SSLMODE"]) == ("55432", "disable")
    with pytest.raises(TransformError, match="no host"):
        connection_variables("postgresql:///radar")


def test_secrets_are_scrubbed_from_messages():
    secrets = runner.secrets_of(connection_variables(URL))
    text = (
        'connection to server at "ep-example-123.eu-central-1.aws.neon.tech" failed for user owner_user\n(s3cret-pass)'
    )
    assert scrub(text, secrets) == 'connection to server at "***" failed for user *** (***)'
    assert scrub("a b", ["a"]) == "a b"  # values too short to be secrets are left alone


def test_dbt_results_become_node_results():
    result = SimpleNamespace(
        results=[
            SimpleNamespace(
                node=SimpleNamespace(resource_type="test", name="not_null_fct_price_usd"),
                status="fail",
                message="Got 3 results, configured to fail if != 0 at ep-example-123.eu-central-1.aws.neon.tech",
                failures=3,
                execution_time=0.42,
            )
        ]
    )
    (item,) = node_results(result, ["ep-example-123.eu-central-1.aws.neon.tech"])
    assert item == NodeResult(
        "test", "not_null_fct_price_usd", "fail", "Got 3 results, configured to fail if != 0 at ***", 3, 0.4
    )
    assert node_results(None, []) == []


def test_a_clean_build_is_one_line_and_ok():
    report = BuildReport(True, [node(), node("test", "unique_x", "pass", "")], 12.3)
    assert report_lines(report) == ["dbt build: 1 models, 1 tests; 1 pass, 1 success (12 s)"]
    assert (report.failed, report.warned, status_word(report)) == (False, False, "OK")
    assert annotations(report) == ["::notice title=dbt build::dbt build: 1 models, 1 tests; 1 pass, 1 success (12 s)"]
    assert markdown(report).startswith("### dbt build\n\n- dbt build:")


def test_warnings_and_failures_are_listed_and_set_the_status():
    warned = BuildReport(True, [node(), node("test", "relationships_device_id", "warn", "Got 2 results", 2)], 5)
    assert report_lines(warned)[1] == "warn test relationships_device_id (2 rows): Got 2 results"
    assert (warned.failed, status_word(warned)) == (False, "WARNING")
    assert annotations(warned)[0].startswith("::warning title=dbt build::")

    failed = BuildReport(
        False,
        [
            node(status="error", message="Database Error: division by zero"),
            node("model", "daily_market", "skipped", ""),
        ],
        5,
    )
    assert report_lines(failed)[1:] == [
        "error model fct_offer_daily: Database Error: division by zero",
        "skipped after a failure: daily_market",
    ]
    assert (failed.failed, status_word(failed)) == (True, "ALERT")
    assert BuildReport(True, [], 1, error="ProjectError: no profile").failed


def test_main_needs_a_database_url(monkeypatch, capsys):
    monkeypatch.setattr(runner, "database_url", lambda: None)
    assert runner.main(["build"]) == 2
    monkeypatch.setattr(runner, "database_url", lambda: "host=localhost dbname=radar user='unclosed")
    assert runner.main(["build"]) == 2
    assert "unclosed" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("report", "code", "sent"),
    [
        (BuildReport(True, [node()], 1), 0, None),
        (BuildReport(True, [node("test", "relationships_device_id", "warn", "Got 1 result", 1)], 1), 0, "[WARNING]"),
        (BuildReport(False, [node(status="error", message="boom")], 1), 1, "[ALERT]"),
    ],
)
def test_main_reports_and_alerts_on_problems(monkeypatch, tmp_path, capsys, report, code, sent):
    messages = []
    monkeypatch.setattr(runner, "database_url", lambda: URL)
    monkeypatch.setattr(runner, "build", lambda variables, **_: report)
    monkeypatch.setattr(runner.telegram, "send", lambda text: messages.append(text) or True)
    summary = tmp_path / "summary.md"
    assert runner.main(["build", "--summary", str(summary), "--annotate"]) == code
    out = capsys.readouterr().out
    assert "s3cret-pass" not in out and "ep-example-123" not in out
    assert summary.read_text(encoding="utf-8").startswith("### dbt build")
    assert [m.split(" ", 1)[0] for m in messages] == ([sent] if sent else [])


def test_an_unexpected_failure_is_scrubbed_and_alerts(monkeypatch, capsys):
    messages = []

    def broken(variables, **_):
        raise RuntimeError(f"could not connect to {variables['DBT_ENV_SECRET_HOST']}")

    monkeypatch.setattr(runner, "database_url", lambda: URL)
    monkeypatch.setattr(runner, "build", broken)
    monkeypatch.setattr(runner.telegram, "send", lambda text: messages.append(text) or True)
    assert runner.main(["build"]) == 1
    assert "error: RuntimeError: could not connect to ***" in capsys.readouterr().out
    assert messages and "ep-example-123" not in messages[0]
