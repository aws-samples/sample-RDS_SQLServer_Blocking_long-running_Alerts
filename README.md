# Automated Blocking & Long-Running Query Alerts for Amazon RDS for SQL Server

## Project Goal

Build a serverless monitoring solution that detects **blocking chains** and
**long-running queries** on Amazon RDS for SQL Server in real time, and sends
HTML-formatted email alerts when configurable thresholds are exceeded.

On Amazon RDS for SQL Server, SQL Server Agent is available but does not
natively publish to Amazon CloudWatch or Amazon SES. This project fills that
gap with an AWS Lambda function that queries Dynamic Management Views (DMVs)
every minute, identifies the **root blocker** in a blocking chain using a
recursive Common Table Expression (CTE), excludes system background processes,
publishes custom CloudWatch metrics, and emails a readable HTML table of the
affected sessions.

## Architecture

![Serverless blocking and long-running query alert architecture](Architecture%20diagram/rds-sqlserver-blocking-alerts.drawio.png)

**Alert flow:** Amazon EventBridge (every 1 minute) → AWS Lambda queries DMVs →
Amazon CloudWatch metrics + alarms → Amazon SES HTML email.

The Lambda function runs inside your VPC (private subnets with a NAT route),
retrieves credentials from AWS Secrets Manager, and connects to RDS on port
1433 using the pure-Python `pytds` library.

## How Blocking Detection Works

Blocking occurs when Session A holds a lock and Session B requests a conflicting
lock on the same resource. In `sys.dm_exec_requests`, blocked sessions have a
non-zero `blocking_session_id`. In production, blocking forms chains
(A blocks B blocks C). A recursive CTE walks the `blocking_session_id` chain
back to the session whose `blocking_session_id = 0` — the **root blocker**.

![Recursive CTE identifies the root blocker](Architecture%20diagram/recursive-cte-root-blocker.drawio.png)

Long-running detection uses `total_elapsed_time`, restricts to user sessions
(`session_id > 50`), and excludes background commands (CDC scans, broker tasks,
ghost cleanup, checkpoint, etc.) to avoid false positives.

### Key DMVs

| DMV | Purpose | Key columns |
|-----|---------|-------------|
| `sys.dm_exec_requests` | Active requests with blocking info | `session_id`, `blocking_session_id`, `wait_time`, `total_elapsed_time` |
| `sys.dm_exec_sessions` | Session details | `login_name`, `host_name`, `is_user_process` |
| `sys.dm_exec_connections` | Connection SQL handles | `most_recent_sql_handle` |
| `sys.dm_exec_sql_text()` | Query text from a handle | `text` |

## Project Structure

```
Architecture diagram/
  rds-sqlserver-blocking-alerts.drawio(.png)   # Solution architecture
  recursive-cte-root-blocker.drawio(.png)      # Blocking-chain / root-blocker diagram

Infrastructure-As-Code/
  template.yaml                                # AWS SAM template (Lambda, schedule, alarms, IAM)

scripts/
  package_and_deploy.sh                        # Build + deploy via AWS SAM
  deploy_cli.sh                                # Manual deploy via raw AWS CLI (blog path)
  cleanup.sh                                   # Remove resources created by the CLI path

src/
  lambda_function.py                           # Lambda handler: DMV queries, metrics, SES email

tests/
  unit/test_lambda_function.py                 # Unit tests (no AWS/SQL Server required)
  sql/simulate_long_running.sql                # Long-running query simulation
  sql/simulate_blocking.sql                    # Blocking-chain simulation

Requirements.txt                               # Runtime + test dependencies
```

## Prerequisites

* An Amazon RDS for SQL Server instance (Standard or Enterprise, version 2016 or later)
* A VPC with private subnets that route to a NAT Gateway (so Lambda can reach AWS services)
* A security group allowing the Lambda function to connect to RDS on **TCP 1433**
* An AWS Secrets Manager secret with the RDS credentials as JSON: `{"username":"...","password":"..."}`
* Amazon SES with **verified** sender and recipient email addresses (verify both if your account is in the SES sandbox)
* Python 3.12 Lambda runtime; the `python-tds` (`pytds`) library packaged in the deployment ZIP

> **Why pytds?** `pytds` is a pure-Python TDS implementation — no ODBC driver or
> C extension — so it packages directly into a Lambda ZIP (~89 KB) with no layers.

## Environment Variables

| Variable | Description | Example |
|----------|-------------|---------|
| `RDS_ENDPOINT` | RDS instance endpoint (host) | `mydb.abc123.eu-west-2.rds.amazonaws.com` |
| `RDS_SECRET_ARN` | Secrets Manager secret ARN | `arn:aws:secretsmanager:eu-west-2:123456:secret:rds-creds` |
| `BLOCKING_THRESHOLD_SECONDS` | Blocking alert threshold | `30` |
| `LONG_RUNNING_THRESHOLD_SECONDS` | Long-running query threshold | `300` |
| `DB_IDENTIFIER` | RDS instance identifier (metric dimension) | `my-rds-instance` |
| `SES_SENDER` | Verified SES sender | `alerts@example.com` |
| `SES_RECIPIENT` | Alert recipient | `dba@example.com` |

## Deployment

### Option A — AWS SAM (recommended)

```bash
./scripts/package_and_deploy.sh \
  --stack-name rds-blocking-alerts \
  --region eu-west-2 \
  --s3-bucket <DEPLOYMENT_BUCKET> \
  --rds-endpoint mydb.abc123.eu-west-2.rds.amazonaws.com \
  --rds-secret-arn arn:aws:secretsmanager:eu-west-2:123456:secret:rds-creds \
  --db-identifier my-rds-instance \
  --subnet-ids subnet-aaa,subnet-bbb \
  --sg-ids sg-zzz \
  --ses-sender alerts@example.com \
  --ses-recipient dba@example.com
```

The SAM template (`Infrastructure-As-Code/template.yaml`) provisions the Lambda
function (with a least-privilege IAM role scoped to the specific secret and the
`RDS/SQLServer/Blocking` metric namespace), the EventBridge `rate(1 minute)`
schedule, and the two CloudWatch alarms.

### Option B — Manual (raw AWS CLI, mirrors the blog steps)

**Step 1 — Store credentials in AWS Secrets Manager**

> **Security note (CWE-798 / CWE-214):** Do **not** pass the password inline on
> the command line (e.g. `--secret-string '{"username":"admin","password":"..."}'`).
> Inline secrets are exposed in your shell history and process list. Instead,
> write the credentials to a temporary file with restrictive permissions and
> reference it with `file://`, then delete it.

```bash
# Create the credentials file with owner-only permissions (never world-readable).
umask 077
cat > /tmp/rds-creds.json <<'JSON'
{"username":"admin","password":"REPLACE_WITH_YOUR_PASSWORD"}
JSON

aws secretsmanager create-secret \
  --name rds-sqlserver-blocking-creds \
  --description "Credentials for Amazon RDS for SQL Server blocking monitor" \
  --secret-string file:///tmp/rds-creds.json \
  --region eu-west-2

# Remove the temporary credentials file immediately afterward.
rm -f /tmp/rds-creds.json
```

> Prefer a strong, unique password (or manage it with an
> [AWS Secrets Manager rotation](https://docs.aws.amazon.com/secretsmanager/latest/userguide/rotating-secrets.html)).
> In production, create the secret through infrastructure-as-code or a
> controlled pipeline rather than a developer workstation.

**Step 2 — Verify email addresses in Amazon SES**
```bash
aws ses verify-email-identity --email-address sender@example.com --region eu-west-2
aws ses verify-email-identity --email-address recipient@example.com --region eu-west-2
```

**Steps 3–5 — Create the function, schedule, and alarms** in one shot:
```bash
./scripts/deploy_cli.sh \
  --account-id 123456789012 \
  --region eu-west-2 \
  --role-name rds-blocking-check-lambda-role \
  --subnet-ids subnet-xxx,subnet-yyy \
  --sg-ids sg-zzz \
  --rds-endpoint mydb.abc123.eu-west-2.rds.amazonaws.com \
  --rds-secret-arn arn:aws:secretsmanager:eu-west-2:123456:secret:rds-creds \
  --db-identifier my-rds-instance \
  --ses-sender alerts@example.com \
  --ses-recipient dba@example.com
```

## Testing / Simulation

**Unit tests** (no AWS or SQL Server needed):
```bash
python -m pytest tests/unit -v
```

**Long-running query** — run `tests/sql/simulate_long_running.sql` against a
non-production database; the cross-join runs past the threshold and triggers a
detection on the next invocation.

**Blocking chain** — open two SSMS sessions and follow
`tests/sql/simulate_blocking.sql`. Session 1 holds locks in an uncommitted
transaction; Session 2 blocks on it. After ~60 seconds the function identifies
Session 1 as the root blocker and emails the HTML table. Release with
`ROLLBACK TRANSACTION;`.

## Cost Estimate

| Service | Usage | Monthly cost (USD) |
|---------|-------|--------------------|
| AWS Lambda | 43,200 invocations (1/min), 256 MB, ~5 s each | ~$0.50 |
| Amazon CloudWatch custom metrics | 3 metrics | ~$0.90 |
| Amazon CloudWatch alarms | 2 alarms | ~$0.20 |
| Amazon EventBridge | 43,200 invocations | ~$0.04 |
| Amazon SES | Email alerts (on demand) | ~$0.00 |
| AWS Secrets Manager | 1 secret | ~$0.40 |
| **Total** | | **~$2.04/month** |

## Clean Up

If you deployed with SAM:
```bash
aws cloudformation delete-stack --stack-name rds-blocking-alerts --region eu-west-2
```

If you deployed with the manual CLI path:
```bash
./scripts/cleanup.sh --region eu-west-2
```

## Extending the Solution

* Add multiple recipients in Amazon SES
* Build a CloudWatch dashboard to visualize blocking trends over time
* Route alerts to Slack via AWS Chatbot

## References

* [Monitoring Amazon RDS metrics with Amazon CloudWatch](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/monitoring-cloudwatch.html)
* [sys.dm_exec_requests (Transact-SQL)](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-exec-requests-transact-sql)
* [Understand and resolve SQL Server blocking problems](https://learn.microsoft.com/en-us/troubleshoot/sql/database-engine/performance/understand-resolve-blocking)
* [Amazon SES: Verifying email addresses](https://docs.aws.amazon.com/ses/latest/dg/creating-identities.html)
* [Using AWS Lambda with Amazon VPC](https://docs.aws.amazon.com/lambda/latest/dg/configuration-vpc.html)
* [python-tds (pytds) on PyPI](https://pypi.org/project/python-tds/)

## Authors

* **Sreedhar Reddy Bakkireddy** — Database Specialist, AWS
* **Manish Dudi** — Database Specialist, AWS
* **Dinakaran Joseph Mulkalpally** — Database Specialist, AWS

## Security

See [CONTRIBUTING](CONTRIBUTING.md#security-issue-notifications) for security issue reporting.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
