#!/usr/bin/env bash
# Print the MCP server's environment variables from build/outputs.json.
# Used to run the server locally and to build the Runtime's EnvironmentVariables block.
#   eval "$(./scripts/server_env.sh --export)"    # load into the current shell
#   ./scripts/server_env.sh --json               # feed the CloudFormation parameter
set -euo pipefail
source "$(dirname "$0")/lib.sh"

out_require glue_database athena_workgroup analytics_role_arn business_role_arn

emit() { printf '%s\t%s\n' "$1" "$2"; }

PAIRS=$(
  emit GLUE_DATABASE       "$(out_get glue_database)"
  emit ATHENA_WORKGROUP    "$(out_get athena_workgroup)"
  emit ANALYTICS_ROLE_ARN  "$(out_get analytics_role_arn)"
  emit BUSINESS_ROLE_ARN   "$(out_get business_role_arn)"
  emit SERVER_REGION       "${AWS_REGION}"
  emit ANALYTICS_MODEL_IDS "anthropic.claude-sonnet-4-20250514-v1:0,anthropic.claude-3-5-haiku-20241022-v1:0"
  emit BUSINESS_MODEL_IDS  "anthropic.claude-3-5-haiku-20241022-v1:0"
)

case "${1:---export}" in
  --export)
    while IFS=$'\t' read -r k v; do printf 'export %s=%q\n' "$k" "$v"; done <<< "${PAIRS}"
    ;;
  --json)
    python3 -c '
import json,sys
print(json.dumps(dict(l.split("\t",1) for l in sys.stdin.read().splitlines() if l.strip())))
' <<< "${PAIRS}"
    ;;
  --cfn)
    # CloudFormation CommaDelimitedList-safe KEY=VALUE list is awkward for ARNs,
    # so the AgentCore template takes the JSON form instead.
    python3 -c '
import json,sys
print(json.dumps(dict(l.split("\t",1) for l in sys.stdin.read().splitlines() if l.strip())))
' <<< "${PAIRS}"
    ;;
  *) die "usage: server_env.sh [--export|--json]" ;;
esac
