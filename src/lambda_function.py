"""
Amazon RDS for SQL Server - Blocking & Long-Running Query Alerts.

This AWS Lambda function connects to an Amazon RDS for SQL Server instance,
queries Dynamic Management Views (DMVs) to detect:

  1. Blocking chains - identifying the *root blocker* via a recursive CTE.
  2. Long-running queries - excluding SQL Server internal background processes.

It publishes custom metrics to Amazon CloudWatch and, when either condition is
detected, sends an HTML-formatted email alert through Amazon SES.

Runtime : Python 3.12
Depends : python-tds (pytds), boto3 (bundled in the Lambda runtime)
"""

import datetime
import html
import json
import os

import boto3
import pytds

# --------------------------------------------------------------------------- #
# SQL: Blocking detection with root-blocker identification (recursive CTE)
# --------------------------------------------------------------------------- #
BLOCKING_QUERY = """
;WITH BlockingChain AS (
    SELECT session_id, blocking_session_id, session_id AS root_blocker
    FROM sys.dm_exec_requests
    WHERE blocking_session_id = 0 AND session_id IN (
        SELECT blocking_session_id FROM sys.dm_exec_requests
        WHERE blocking_session_id > 0)
    UNION ALL
    SELECT r.session_id, r.blocking_session_id, bc.root_blocker
    FROM sys.dm_exec_requests r
    INNER JOIN BlockingChain bc ON r.blocking_session_id = bc.session_id
    WHERE r.blocking_session_id > 0
)
SELECT r.session_id AS blocked_session,
    r.blocking_session_id AS blocker_session,
    ISNULL(bc.root_blocker, r.blocking_session_id) AS root_blocker,
    r.wait_type, r.wait_time / 1000 AS wait_time_seconds,
    DB_NAME(r.database_id) AS database_name,
    SUBSTRING(blocked_text.text, 1, 300) AS blocked_query,
    SUBSTRING(ISNULL(blocker_text.text, ''), 1, 300) AS blocker_query,
    s_blocked.login_name AS blocked_login
FROM sys.dm_exec_requests r
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) blocked_text
JOIN sys.dm_exec_sessions s_blocked ON r.session_id = s_blocked.session_id
LEFT JOIN sys.dm_exec_connections c_blocker
    ON r.blocking_session_id = c_blocker.session_id
OUTER APPLY sys.dm_exec_sql_text(c_blocker.most_recent_sql_handle) blocker_text
LEFT JOIN BlockingChain bc ON r.session_id = bc.session_id
WHERE r.blocking_session_id > 0 AND r.wait_time > %s
ORDER BY r.wait_time DESC
"""

# --------------------------------------------------------------------------- #
# SQL: Long-running query detection (excludes system background processes)
# --------------------------------------------------------------------------- #
LONG_RUNNING_QUERY = """
SELECT r.session_id,
    r.total_elapsed_time / 1000 AS duration_seconds,
    r.status, r.command,
    DB_NAME(r.database_id) AS database_name,
    s.login_name, SUBSTRING(t.text, 1, 500) AS query_text
FROM sys.dm_exec_requests r
JOIN sys.dm_exec_sessions s ON r.session_id = s.session_id
CROSS APPLY sys.dm_exec_sql_text(r.sql_handle) t
WHERE r.session_id > 50
    AND r.total_elapsed_time > %s
    AND s.login_name NOT LIKE '%%NT AUTHORITY%%'
    AND s.login_name NOT LIKE '%%NT SERVICE%%'
    AND r.command NOT IN ('CDC_SCAN','BRKR TASK','GHOST CLEANUP',
        'CHECKPOINT','XE TIMER','XE DISPATCHER',
        'BROKER_EVENTHANDLER','DBCC')
ORDER BY r.total_elapsed_time DESC
"""

CLOUDWATCH_NAMESPACE = "RDS/SQLServer/Blocking"


def get_credentials(secret_arn):
    """Retrieve RDS credentials from AWS Secrets Manager."""
    client = boto3.client("secretsmanager")
    response = client.get_secret_value(SecretId=secret_arn)
    return json.loads(response["SecretString"])


def _cell(value):
    """Return an HTML-escaped table cell for the given value."""
    return f"<td>{html.escape(str(value if value is not None else ''))}</td>"


def format_html_alert(db_identifier, blocked_rows, long_running_rows):
    """Format the alert as an HTML table for email readability."""
    detected_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    parts = [
        '<html><body style="font-family:Arial;">',
        f"<h2>RDS Alert: {html.escape(db_identifier)}</h2>",
        f"<p>Detected at: {detected_at}</p>",
    ]

    if blocked_rows:
        parts.append("<h3>Blocking Detected</h3>")
        parts.append(
            '<table border="1" cellpadding="8" style="border-collapse:collapse;">'
            "<tr><th>Blocked</th><th>Blocker</th><th>Root Blocker</th>"
            "<th>Wait(s)</th><th>Database</th><th>Blocked Query</th>"
            "<th>Blocker Query</th></tr>"
        )
        for r in blocked_rows:
            parts.append(
                "<tr>"
                + _cell(r.get("blocked_session"))
                + _cell(r.get("blocker_session"))
                + _cell(r.get("root_blocker"))
                + _cell(r.get("wait_time_seconds"))
                + _cell(r.get("database_name"))
                + _cell(r.get("blocked_query"))
                + _cell(r.get("blocker_query"))
                + "</tr>"
            )
        parts.append("</table>")

    if long_running_rows:
        parts.append("<h3>Long-Running Queries</h3>")
        parts.append(
            '<table border="1" cellpadding="8" style="border-collapse:collapse;">'
            "<tr><th>Session</th><th>Duration(s)</th><th>Database</th>"
            "<th>Login</th><th>Query</th></tr>"
        )
        for r in long_running_rows:
            parts.append(
                "<tr>"
                + _cell(r.get("session_id"))
                + _cell(r.get("duration_seconds"))
                + _cell(r.get("database_name"))
                + _cell(r.get("login_name"))
                + _cell(r.get("query_text"))
                + "</tr>"
            )
        parts.append("</table>")

    parts.append("</body></html>")
    return "".join(parts)


def _rows_as_dicts(cursor):
    """Convert the current cursor result set into a list of dicts."""
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def publish_metrics(cloudwatch, db_identifier, blocking_count, long_running_count):
    """Publish blocking and long-running counts to Amazon CloudWatch."""
    dimensions = [{"Name": "DBInstanceIdentifier", "Value": db_identifier}]
    cloudwatch.put_metric_data(
        Namespace=CLOUDWATCH_NAMESPACE,
        MetricData=[
            {
                "MetricName": "BlockedSessionCount",
                "Value": blocking_count,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
            {
                "MetricName": "LongRunningQueryCount",
                "Value": long_running_count,
                "Unit": "Count",
                "Dimensions": dimensions,
            },
        ],
    )


def send_alert(db_identifier, blocked_rows, long_running_rows, sender, recipient):
    """Send an HTML alert email through Amazon SES."""
    if not (sender and recipient):
        print("SES_SENDER / SES_RECIPIENT not set - skipping email alert.")
        return
    subject = f"RDS ALERT: {db_identifier}"
    html_message = format_html_alert(db_identifier, blocked_rows, long_running_rows)
    boto3.client("ses").send_email(
        Source=sender,
        Destination={"ToAddresses": [recipient]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Html": {"Data": html_message}},
        },
    )


def handler(event, context):
    """Lambda entry point."""
    cloudwatch = boto3.client("cloudwatch")

    server = os.environ["RDS_ENDPOINT"]
    secret_arn = os.environ["RDS_SECRET_ARN"]
    blocking_threshold = int(os.environ.get("BLOCKING_THRESHOLD_SECONDS", "30"))
    long_running_threshold = int(os.environ.get("LONG_RUNNING_THRESHOLD_SECONDS", "300"))
    db_identifier = os.environ.get("DB_IDENTIFIER", "my-rds-instance")
    ses_sender = os.environ.get("SES_SENDER", "")
    ses_recipient = os.environ.get("SES_RECIPIENT", "")

    credentials = get_credentials(secret_arn)

    # ----------------------------------------------------------------------- #
    # Transport encryption (TLS) for data in transit  (Finding: CWE-319).
    #
    # How pytds handles TLS: encryption is negotiated ONLY when a trusted CA
    # bundle is supplied via `cafile=`. With `cafile` set, pytds validates the
    # server certificate (validate_host=True by default), which both encrypts
    # the connection and prevents man-in-the-middle attacks. Enabling TLS in
    # pytds also requires the pyOpenSSL extra: install `python-tds[tls]`.
    #
    # BLOG SAMPLE: To keep the sample self-contained (no extra CA file to
    # download to follow along), `RDS_CA_BUNDLE_PATH` is unset by default and
    # the connection is made without a bundled CA. This is acceptable ONLY for
    # a demo in a private VPC/subnet.
    #
    # PRODUCTION: Package the Amazon RDS CA bundle with the function and set
    # the RDS_CA_BUNDLE_PATH env var to its path, e.g.
    #     RDS_CA_BUNDLE_PATH=/var/task/rds-combined-ca-bundle.pem
    # and add `python-tds[tls]` to the deployment package. Also enforce
    # `rds.force_ssl` on the DB parameter group so the server itself REJECTS
    # any unencrypted connection. See:
    #   https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/SQLServer.Concepts.General.SSL.Using.html
    # ----------------------------------------------------------------------- #
    rds_ca_bundle = os.environ.get("RDS_CA_BUNDLE_PATH", "")
    connect_kwargs = {
        "user": credentials["username"],
        "password": credentials["password"],
        "database": "master",
        "port": 1433,
        "login_timeout": 10,
        "timeout": 15,
    }
    if rds_ca_bundle:
        # Production path: TLS with full server-certificate validation.
        # (validate_host defaults to True in pytds; set explicitly for clarity.)
        connect_kwargs["cafile"] = rds_ca_bundle
        connect_kwargs["validate_host"] = True

    try:
        with pytds.connect(server, **connect_kwargs) as conn:
            cursor = conn.cursor()
            cursor.execute(BLOCKING_QUERY, (blocking_threshold * 1000,))
            blocked_rows = _rows_as_dicts(cursor)
            cursor.execute(LONG_RUNNING_QUERY, (long_running_threshold * 1000,))
            long_running_rows = _rows_as_dicts(cursor)
    except Exception as exc:  # noqa: BLE001 - surface any connection/query failure
        print(f"Connection or query error: {exc}")
        return {"blocking_count": 0, "long_running_count": 0, "error": str(exc)}

    blocking_count = len(blocked_rows)
    long_running_count = len(long_running_rows)

    publish_metrics(cloudwatch, db_identifier, blocking_count, long_running_count)

    if blocking_count > 0 or long_running_count > 0:
        send_alert(
            db_identifier,
            blocked_rows,
            long_running_rows,
            ses_sender,
            ses_recipient,
        )

    return {
        "blocking_count": blocking_count,
        "long_running_count": long_running_count,
    }
