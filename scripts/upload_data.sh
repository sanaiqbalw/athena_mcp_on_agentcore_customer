#!/usr/bin/env bash
# Sync the generated Parquet to S3 and register the transactions partitions in Glue.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

out_require data_bucket glue_database
BUCKET="$(out_get data_bucket)"
DB="$(out_get glue_database)"

[[ -d "${BUILD_DIR}/data" ]] || die "no generated data; run scripts/gen_data.py first"

log "syncing Parquet to s3://${BUCKET}/data/"
aws_ s3 sync "${BUILD_DIR}/data" "s3://${BUCKET}/data/" \
  --exclude "partitions.txt" --delete --only-show-errors
ok "upload complete"

log "registering transactions partitions in Glue"
python3 "${ROOT_DIR}/scripts/register_partitions.py" \
  --bucket "${BUCKET}" --database "${DB}" --table transactions \
  --partitions-file "${BUILD_DIR}/data/partitions.txt"
