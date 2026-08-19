#!/usr/bin/env bash
#
# cleanup.sh
# Remove the resources created by the manual (CLI) deployment path.
# If you deployed with SAM/CloudFormation, delete the stack instead:
#   aws cloudformation delete-stack --stack-name rds-blocking-alerts --region <REGION>
#
# Usage:
#   ./scripts/cleanup.sh --region eu-west-2
#
set -euo pipefail

REGION="eu-west-2"
FUNCTION_NAME="rds-blocking-check"
RULE_NAME="rds-blocking-check-schedule"
SECRET_ID="rds-sqlserver-blocking-creds"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --region) REGION="$2"; shift 2 ;;
    --secret-id) SECRET_ID="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

echo "==> Disabling and removing EventBridge rule"
aws events remove-targets --rule "${RULE_NAME}" --ids 1 --region "${REGION}" || true
aws events delete-rule --name "${RULE_NAME}" --region "${REGION}" || true

echo "==> Deleting Lambda function"
aws lambda delete-function --function-name "${FUNCTION_NAME}" --region "${REGION}" || true

echo "==> Deleting CloudWatch alarms"
aws cloudwatch delete-alarms \
  --alarm-names RDS-SQLServer-Blocking-Detected RDS-SQLServer-LongRunningQuery-Detected \
  --region "${REGION}" || true

echo "==> Deleting Secrets Manager secret (force, no recovery window)"
aws secretsmanager delete-secret --secret-id "${SECRET_ID}" \
  --force-delete-without-recovery --region "${REGION}" || true

echo "==> Cleanup complete."
