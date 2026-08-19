#!/usr/bin/env bash
#
# package_and_deploy.sh
# Package the Lambda function with its pytds dependency and deploy the
# blocking-alerts solution via AWS SAM (CloudFormation).
#
# Usage:
#   ./scripts/package_and_deploy.sh \
#     --stack-name rds-blocking-alerts \
#     --region eu-west-2 \
#     --s3-bucket <DEPLOYMENT_BUCKET> \
#     --rds-endpoint mydb.abc123.eu-west-2.rds.amazonaws.com \
#     --rds-secret-arn arn:aws:secretsmanager:eu-west-2:123456:secret:rds-creds \
#     --db-identifier my-rds-instance \
#     --subnet-ids subnet-aaa,subnet-bbb \
#     --sg-ids sg-zzz \
#     --ses-sender alerts@example.com \
#     --ses-recipient dba@example.com
#
set -euo pipefail

STACK_NAME="rds-blocking-alerts"
REGION="eu-west-2"
S3_BUCKET=""
RDS_ENDPOINT=""
RDS_SECRET_ARN=""
DB_IDENTIFIER="my-rds-instance"
SUBNET_IDS=""
SG_IDS=""
SES_SENDER=""
SES_RECIPIENT=""
BLOCKING_THRESHOLD=30
LONG_RUNNING_THRESHOLD=300

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
TEMPLATE="${ROOT_DIR}/Infrastructure-As-Code/template.yaml"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name) STACK_NAME="$2"; shift 2 ;;
    --region) REGION="$2"; shift 2 ;;
    --s3-bucket) S3_BUCKET="$2"; shift 2 ;;
    --rds-endpoint) RDS_ENDPOINT="$2"; shift 2 ;;
    --rds-secret-arn) RDS_SECRET_ARN="$2"; shift 2 ;;
    --db-identifier) DB_IDENTIFIER="$2"; shift 2 ;;
    --subnet-ids) SUBNET_IDS="$2"; shift 2 ;;
    --sg-ids) SG_IDS="$2"; shift 2 ;;
    --ses-sender) SES_SENDER="$2"; shift 2 ;;
    --ses-recipient) SES_RECIPIENT="$2"; shift 2 ;;
    --blocking-threshold) BLOCKING_THRESHOLD="$2"; shift 2 ;;
    --long-running-threshold) LONG_RUNNING_THRESHOLD="$2"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

for req in S3_BUCKET RDS_ENDPOINT RDS_SECRET_ARN SUBNET_IDS SG_IDS; do
  if [[ -z "${!req}" ]]; then
    echo "ERROR: --${req,,} is required (use dashes, e.g. --s3-bucket)." >&2
    exit 1
  fi
done

echo "==> Building with SAM (installs pytds into the build artifact)"
sam build --template-file "${TEMPLATE}"

echo "==> Deploying stack '${STACK_NAME}' to ${REGION}"
sam deploy \
  --stack-name "${STACK_NAME}" \
  --region "${REGION}" \
  --s3-bucket "${S3_BUCKET}" \
  --capabilities CAPABILITY_IAM \
  --no-confirm-changeset \
  --parameter-overrides \
    RdsEndpoint="${RDS_ENDPOINT}" \
    RdsSecretArn="${RDS_SECRET_ARN}" \
    DbIdentifier="${DB_IDENTIFIER}" \
    VpcSubnetIds="${SUBNET_IDS}" \
    SecurityGroupIds="${SG_IDS}" \
    SesSender="${SES_SENDER}" \
    SesRecipient="${SES_RECIPIENT}" \
    BlockingThresholdSeconds="${BLOCKING_THRESHOLD}" \
    LongRunningThresholdSeconds="${LONG_RUNNING_THRESHOLD}"

echo "==> Done. Review stack outputs above."
