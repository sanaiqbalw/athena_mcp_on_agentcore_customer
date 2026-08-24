#!/usr/bin/env bash
# Reverse of deploy.sh. Deletes what this demo created and nothing else.
#
#   ./teardown.sh --dry-run      show what would be deleted, touch nothing
#   ./teardown.sh                delete, prompting before the destructive bits
#   ./teardown.sh --yes          no prompts (CI)
#   ./teardown.sh --keep-bucket  leave the S3 data in place
#
# Two things this is careful about, because they are shared account state:
#   * Lake Formation administrators. We remove our own principal ONLY if
#     build/outputs.json records that we added it. Administrators that were already
#     there when we arrived are never touched.
#   * Account-level CreateDatabaseDefaultPermissions / CreateTableDefaultPermissions.
#     Never modified, on the way in or the way out.
set -euo pipefail
source "$(dirname "$0")/scripts/lib.sh"

DRY_RUN=0; ASSUME_YES=0; KEEP_BUCKET=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --keep-bucket) KEEP_BUCKET=1; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Reverse of the deploy order. A gateway target must go before the gateway it attaches to,
# which is why they are separate stacks. delete_stack treats a missing stack as already
# done, so this is safe on a partial deploy.
STACKS=(
  "${STACK_TARGET}"
  "${STACK_GATEWAY}"
  "${STACK_RUNTIME}"
  "${STACK_INTERCEPTOR}"
  "${STACK_LF}"
  "${STACK_ROLES}"
  "${STACK_ECR}"
  "${STACK_DATALAKE}"
)

confirm() {
  # A dry run must never block on input, and never needs consent - it changes nothing.
  [[ ${DRY_RUN} -eq 1 || ${ASSUME_YES} -eq 1 ]] && return 0
  local reply
  read -r -p "$1 [y/N] " reply
  [[ "${reply}" =~ ^[Yy]$ ]]
}

run() {
  if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "   would run: $*"
    return 0
  fi
  "$@"
}

delete_stack() {
  local stack="$1" status
  status="$(stack_status "${stack}")"
  if [[ "${status}" == "MISSING" ]]; then
    ok "${stack} already gone"
    return 0
  fi
  log "deleting ${stack} (${status})"
  if [[ ${DRY_RUN} -eq 1 ]]; then
    echo "   would delete stack ${stack}"
    return 0
  fi
  aws_ cloudformation delete-stack --stack-name "${stack}"
  if aws_ cloudformation wait stack-delete-complete --stack-name "${stack}" 2>/dev/null; then
    ok "${stack} deleted"
  else
    warn "${stack} did not reach DELETE_COMPLETE - check the console"
    aws_ cloudformation describe-stack-events --stack-name "${stack}" \
      --query 'StackEvents[?ResourceStatus==`DELETE_FAILED`].[LogicalResourceId,ResourceStatusReason]' \
      --output text 2>/dev/null | head -5 || true
  fi
}

# --- plan ---------------------------------------------------------------------

BUCKET="$(out_get data_bucket)"
LF_APPENDED="$(out_get lf_admin_appended)"

cat <<EOF

$(printf '=%.0s' {1..72})
  Teardown plan $( [[ ${DRY_RUN} -eq 1 ]] && echo '(DRY RUN)' )
$(printf '=%.0s' {1..72})
  account / region   ${ACCOUNT_ID} / ${AWS_REGION}
  stacks to delete   ${#STACKS[@]}
$(for s in "${STACKS[@]}"; do printf '                     %-26s %s\n' "${s}" "$(stack_status "${s}")"; done)
  Kiro mcp.json      remove only this deployment's MCP entry
  LF administrator   $( [[ "${LF_APPENDED}" == "true" ]] && echo "remove ${LF_ADMIN_ARN} (we added it)" || echo 'leave alone (we did not add it)' )
  LF account defaults never modified
  S3 bucket          $( [[ ${KEEP_BUCKET} -eq 1 ]] && echo "KEEP ${BUCKET}" || echo "empty and delete ${BUCKET}" )

EOF

if [[ ${DRY_RUN} -eq 0 ]]; then
  confirm "Delete the resources above?" || die "aborted"
fi

# --- 1. Kiro client config ----------------------------------------------------

log "removing the Kiro MCP entry"
run python3 "${ROOT_DIR}/scripts/write_kiro_config.py" --remove

# --- 2. stacks, in reverse deploy order ---------------------------------------

for stack in "${STACKS[@]}"; do
  delete_stack "${stack}"
done

# --- 3. Lake Formation administrator ------------------------------------------
# After the stacks, because revoking grants and deleting the database need admin.

if [[ "${LF_APPENDED}" == "true" ]]; then
  log "removing ${LF_ADMIN_ARN} from the Lake Formation administrators"
  run python3 "${ROOT_DIR}/scripts/lf_bootstrap.py" \
    --admin-arn "${LF_ADMIN_ARN}" --database "${GLUE_DATABASE}" --remove-admin
else
  ok "we did not add a Lake Formation admin, leaving the list untouched"
fi

# --- 4. the S3 data -----------------------------------------------------------
# The bucket has DeletionPolicy: Retain so the stack delete leaves it behind.
# That is deliberate: losing data should take an explicit extra step.

if [[ ${KEEP_BUCKET} -eq 1 ]]; then
  ok "keeping s3://${BUCKET}"
elif [[ -z "${BUCKET}" ]]; then
  warn "no data_bucket in outputs.json, skipping bucket cleanup"
elif ! aws_ s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1; then
  ok "s3://${BUCKET} already gone"
else
  size="$(aws_ s3 ls "s3://${BUCKET}" --recursive --summarize 2>/dev/null \
          | tail -2 | tr -s ' ' | paste -sd' ' - || echo unknown)"
  warn "s3://${BUCKET} — ${size}"
  if confirm "Permanently delete this bucket and all demo data?"; then
    run aws_ s3 rm "s3://${BUCKET}" --recursive --only-show-errors
    run aws_ s3api delete-bucket --bucket "${BUCKET}"
    if [[ ${DRY_RUN} -eq 0 ]]; then
      ok "bucket deleted"
    fi
  else
    warn "bucket kept — re-run with --keep-bucket to skip this prompt"
  fi
fi

# --- 5. local artefacts -------------------------------------------------------

if [[ ${DRY_RUN} -eq 0 ]]; then
  log "local build artefacts"
  echo "   build/outputs.json and build/demo-credentials.txt are kept so a re-deploy"
  echo "   can still find names. Delete build/ by hand for a clean slate."
fi

cat <<EOF

$(printf '=%.0s' {1..72})
  Teardown complete$( [[ ${DRY_RUN} -eq 1 ]] && echo ' (dry run — nothing changed)' )
$(printf '=%.0s' {1..72})
  Left intentionally in place:
    - Lake Formation administrators that this demo did not add
    - account-level Lake Formation default permissions
    - every other entry in .kiro/settings/mcp.json
$( [[ ${KEEP_BUCKET} -eq 1 ]] && echo "    - s3://${BUCKET}" )

  Verify with:
    aws --profile ${AWS_PROFILE} --region ${AWS_REGION} cloudformation list-stacks \\
      --stack-status-filter CREATE_COMPLETE UPDATE_COMPLETE --query "StackSummaries[?contains(StackName,'${PREFIX}')].StackName"
    aws --profile ${AWS_PROFILE} --region ${AWS_REGION} lakeformation get-data-lake-settings \\
      --query 'DataLakeSettings.DataLakeAdmins'
EOF
