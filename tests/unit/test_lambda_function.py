"""
Unit tests for the RDS for SQL Server blocking-alerts Lambda function.

These tests exercise the pure-Python logic (HTML formatting, metric
publishing, alert dispatch, and the handler flow) using lightweight stubs
so that no real AWS services or SQL Server connection is required.

Run with:  python -m pytest tests/unit -v
"""

import importlib
import os
import sys
import types
from unittest import mock

import pytest

# --------------------------------------------------------------------------- #
# Import src/lambda_function.py, stubbing the optional pytds dependency
# so the module imports cleanly in a bare test environment.
# --------------------------------------------------------------------------- #
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "src")
sys.path.insert(0, os.path.abspath(SRC_DIR))

if "pytds" not in sys.modules:
    sys.modules["pytds"] = types.SimpleNamespace(connect=mock.MagicMock())
if "boto3" not in sys.modules:
    sys.modules["boto3"] = mock.MagicMock()

lambda_function = importlib.import_module("lambda_function")


# --------------------------------------------------------------------------- #
# format_html_alert
# --------------------------------------------------------------------------- #
def test_format_html_alert_includes_blocking_rows():
    blocked = [
        {
            "blocked_session": 66,
            "blocker_session": 55,
            "root_blocker": 55,
            "wait_time_seconds": 42,
            "database_name": "SalesDB",
            "blocked_query": "SELECT * FROM Orders",
            "blocker_query": "UPDATE Orders SET Status='Processing'",
        }
    ]
    html = lambda_function.format_html_alert("my-rds-instance", blocked, [])
    assert "Blocking Detected" in html
    assert "Root Blocker" in html
    assert "SalesDB" in html
    assert "55" in html
    assert "Long-Running Queries" not in html


def test_format_html_alert_includes_long_running_rows():
    long_running = [
        {
            "session_id": 72,
            "duration_seconds": 610,
            "database_name": "SalesDB",
            "login_name": "app_user",
            "query_text": "SELECT ... CROSS JOIN ...",
        }
    ]
    html = lambda_function.format_html_alert("my-rds-instance", [], long_running)
    assert "Long-Running Queries" in html
    assert "app_user" in html
    assert "610" in html


def test_format_html_alert_escapes_html_in_query_text():
    blocked = [
        {
            "blocked_session": 1,
            "blocker_session": 2,
            "root_blocker": 2,
            "wait_time_seconds": 31,
            "database_name": "db",
            "blocked_query": "SELECT '<script>alert(1)</script>'",
            "blocker_query": "",
        }
    ]
    html = lambda_function.format_html_alert("db", blocked, [])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# --------------------------------------------------------------------------- #
# publish_metrics
# --------------------------------------------------------------------------- #
def test_publish_metrics_sends_two_metrics():
    cw = mock.MagicMock()
    lambda_function.publish_metrics(cw, "my-rds-instance", 3, 1)
    cw.put_metric_data.assert_called_once()
    kwargs = cw.put_metric_data.call_args.kwargs
    assert kwargs["Namespace"] == "RDS/SQLServer/Blocking"
    metric_names = {m["MetricName"] for m in kwargs["MetricData"]}
    assert metric_names == {"BlockedSessionCount", "LongRunningQueryCount"}
    values = {m["MetricName"]: m["Value"] for m in kwargs["MetricData"]}
    assert values["BlockedSessionCount"] == 3
    assert values["LongRunningQueryCount"] == 1


# --------------------------------------------------------------------------- #
# send_alert
# --------------------------------------------------------------------------- #
def test_send_alert_skips_when_recipients_missing():
    with mock.patch.object(lambda_function.boto3, "client") as client:
        lambda_function.send_alert("db", [{"blocked_session": 1}], [], "", "")
        client.assert_not_called()


def test_send_alert_calls_ses_when_configured():
    ses = mock.MagicMock()
    with mock.patch.object(lambda_function.boto3, "client", return_value=ses):
        lambda_function.send_alert(
            "db",
            [{"blocked_session": 1, "root_blocker": 1}],
            [],
            "alerts@example.com",
            "dba@example.com",
        )
        ses.send_email.assert_called_once()
        kwargs = ses.send_email.call_args.kwargs
        assert kwargs["Source"] == "alerts@example.com"
        assert kwargs["Destination"]["ToAddresses"] == ["dba@example.com"]
        assert "Html" in kwargs["Message"]["Body"]


# --------------------------------------------------------------------------- #
# handler
# --------------------------------------------------------------------------- #
class _FakeCursor:
    """Minimal cursor returning canned blocking then long-running rows."""

    def __init__(self):
        self._results = [
            (  # blocking result set
                [("blocked_session",), ("blocker_session",), ("root_blocker",)],
                [(66, 55, 55)],
            ),
            (  # long-running result set
                [("session_id",), ("duration_seconds",)],
                [(72, 610)],
            ),
        ]
        self.description = None
        self._rows = []

    def execute(self, _query, _params):
        self.description, self._rows = self._results.pop(0)

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self):
        self._cursor = _FakeCursor()

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RDS_ENDPOINT", "mydb.rds.amazonaws.com")
    monkeypatch.setenv("RDS_SECRET_ARN", "arn:aws:secretsmanager:...:secret")
    monkeypatch.setenv("DB_IDENTIFIER", "my-rds-instance")
    monkeypatch.setenv("SES_SENDER", "")
    monkeypatch.setenv("SES_RECIPIENT", "")


def test_handler_counts_and_publishes(env):
    cw = mock.MagicMock()

    def fake_client(service):
        if service == "cloudwatch":
            return cw
        return mock.MagicMock()

    with mock.patch.object(lambda_function.boto3, "client", side_effect=fake_client), \
        mock.patch.object(
            lambda_function, "get_credentials",
            return_value={"username": "u", "password": "p"},
        ), \
        mock.patch.object(
            lambda_function.pytds, "connect", return_value=_FakeConn(),
        ):
        result = lambda_function.handler({}, None)

    assert result == {"blocking_count": 1, "long_running_count": 1}
    cw.put_metric_data.assert_called_once()


def test_handler_returns_error_on_connection_failure(env):
    with mock.patch.object(lambda_function.boto3, "client", return_value=mock.MagicMock()), \
        mock.patch.object(
            lambda_function, "get_credentials",
            return_value={"username": "u", "password": "p"},
        ), \
        mock.patch.object(
            lambda_function.pytds, "connect",
            side_effect=Exception("network unreachable"),
        ):
        result = lambda_function.handler({}, None)

    assert result["blocking_count"] == 0
    assert result["long_running_count"] == 0
    assert "network unreachable" in result["error"]
