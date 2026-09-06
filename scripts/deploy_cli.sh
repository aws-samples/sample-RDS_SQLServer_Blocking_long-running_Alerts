#!/usr/bin/env bash
#
# deploy_cli.sh
# Deploy the blocking-alerts solution using raw AWS CLI commands
# (the manual path described in the blog post, no CloudFormation).
#
# Prerequisites: an IAM role for the Lambda function already exists.
#
# Usage:
#   ./scripts/deploy_cli.sh \
#     --account-id 123456789012 \
#     --region eu-west-2 \
#     --role-name rds-blocking-check-lambda-role \
#     --subnet-ids subnet-xxx,subnet-yyy \
#     --sg-ids sg-zzz \
#     --rds-endpoint mydb.abc123.eu-west-2.rds.amazonaws.com \
#     --rds-secret-arn arn:aws:secretsmanager:eu-west-2:123456:secret:rds-creds \
#     --db-identifier my-rds-instance \
#     --ses-sender alerts@example.com \
#     --ses-recipient dba@example.com
#
set -euo pipefail

REGION="eu-west-2"
FUNCTION_NAME="rds-blocking-check"
RULE_NAME="rds-blocking-check-schedule"
ROLE_NAME="rds-blocking-check-lambda-role"
DB_IDENTIFIER="my-rds-instance"
BLOCKING_THRESHOLD=60
LONG_RUNNING_THRESHOLD=300
ACCOUNT_ID="" ; SUBNET_IDS="" ; SG_IDS="" ; RDS_ENDPOINT="" ; RDS_SECRET_ARN=""
SES_SENDER="" ; SES_RECIPIENT=""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --account-id) ACCOUNT_ID="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --role-name) ROLE_NAME="$2"; shift 2 ;;
    --subnet-ids) SUBNET_IDS="$2"; shift 2 ;;
    --sg-ids) SG_IDS="$2"; shift 2 ;;
    --rds-endpoint) RDS_ENDPOINT="$2"; shift 2 ;;
    --rds-secret-arn) RDS_SECRET_ARN="$2"; shift 2 ;;
    --db-identifier) DB_IDENTIFIER="$2"; shift 2 ;;
    --ses-sender) SES_SENDER="$2"; shift 2 ;;
    --ses-recipient) SES_RECIPIENT="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

BUILD_DIR="$(mktemp -d)"
echo "==> Packaging function + pytds into ${BUILD_DIR}"
cp "${ROOT_DIR}/src/lambda_function.py" "${BUILD_DIR}/"
pip install python-tds -t "${BUILD_DIR}" >/dev/null
( cd "${BUILD_DIR}" && zip -q -r blocking_check.zip . )

echo "==> Creating Lambda function ${FUNCTION_NAME}"
aws lambda create-function \
  --function-name "${FUNCTION_NAME}" \
  --runtime python3.12 \
  --handler lambda_function.handler \
  --role "arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}" \
  --timeout 60 --memory-size 256 \
  --vpc-config "SubnetIds=${SUBNET_IDS},SecurityGroupIds=${SG_IDS}" \
  --environment "Variables={RDS_ENDPOINT=${RDS_ENDPOINT},RDS_SECRET_ARN=${RDS_SECRET_ARN},DB_IDENTIFIER=${DB_IDENTIFIER},BLOCKING_THRESHOLD_SECONDS=${BLOCKING_THRESHOLD},LONG_RUNNING_THRESHOLD_SECONDS=${LONG_RUNNING_THRESHOLD},SES_SENDER=${SES_SENDER},SES_RECIPIENT=${SES_RECIPIENT}}" \
  --zip-file "fileb://${BUILD_DIR}/blocking_check.zip" \
  --region "${REGION}"

echo "==> Creating EventBridge schedule (rate(1 minute))"
aws events put-rule --name "${RULE_NAME}" \
  --schedule-expression "rate(1 minute)" --state ENABLED --region "${REGION}"

aws lambda add-permission --function-name "${FUNCTION_NAME}" \
  --statement-id EventBridgeInvoke --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
  --region "${REGION}"

aws events put-targets --rule "${RULE_NAME}" \
  --targets "[{\"Id\":\"1\",\"Arn\":\"arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}\"}]" \
  --region "${REGION}"

echo "==> Creating CloudWatch alarms"
aws cloudwatch put-metric-alarm --alarm-name RDS-SQLServer-Blocking-Detected \
  --namespace RDS/SQLServer/Blocking --metric-name BlockedSessionCount \
  --dimensions "Name=DBInstanceIdentifier,Value=${DB_IDENTIFIER}" \
  --statistic Maximum --period 60 --evaluation-periods 1 \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --region "${REGION}"

aws cloudwatch put-metric-alarm --alarm-name RDS-SQLServer-LongRunningQuery-Detected \
  --namespace RDS/SQLServer/Blocking --metric-name LongRunningQueryCount \
  --dimensions "Name=DBInstanceIdentifier,Value=${DB_IDENTIFIER}" \
  --statistic Maximum --period 60 --evaluation-periods 1 \
  --threshold 0 --comparison-operator GreaterThanThreshold \
  --treat-missing-data notBreaching --region "${REGION}"

rm -rf "${BUILD_DIR}"
echo "==> Done."
