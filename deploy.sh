#!/usr/bin/env bash
# One-command deploy of the demo: an Athena MCP server whose callers see different
# columns depending on their Okta group, with Lake Formation doing the filtering.
#
# Idempotent: re-running converges and re-asserts drifted state.
#
#   ./deploy.sh                 everything, in dependency order
#   ./deploy.sh --from image    resume from a step
#   ./deploy.sh --only gateway  a single step
#   ./deploy.sh --list          show the steps
#
# Requires an Okta authorization server configured as described in README.md, and the service
# app's client secret in Secrets Manager. Point config.env at your org before running.
set -euo pipefail
source "$(dirname "$0")/scripts/lib.sh"

STEPS=(preflight data roles lakeformation image interceptor runtime gateway target kiro summary)

usage() { echo "usage: $0 [--from STEP | --only STEP | --skip STEP | --list]"; exit 1; }

FROM=""; ONLY=""; SKIP=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --only) ONLY="${2:-}"; shift 2 ;;
    --skip) SKIP="${SKIP} ${2:-}"; shift 2 ;;
    --list) printf '%s\n' "${STEPS[@]}"; exit 0 ;;
    -h|--help) usage ;;
    *) die "unknown argument: $1" ;;
  esac
done

want() {
  local step="$1"
  [[ -n "${ONLY}" ]] && { [[ "${step}" == "${ONLY}" ]]; return; }
  [[ " ${SKIP} " == *" ${step} "* ]] && return 1
  if [[ -n "${FROM}" ]]; then
    local seen=0 s
    for s in "${STEPS[@]}"; do
      [[ "${s}" == "${FROM}" ]] && seen=1
      [[ "${s}" == "${step}" ]] && { [[ ${seen} -eq 1 ]]; return; }
    done
  fi
  return 0
}

step_preflight() { preflight; }

# --- shared foundation: the data and who may read which columns ----------------

step_data() {
  cfn_deploy "${STACK_DATALAKE}" infra/01-datalake.yaml \
    "DataBucketName=${DATA_BUCKET}" "GlueDatabaseName=${GLUE_DATABASE}" \
    "AthenaWorkgroupName=${ATHENA_WORKGROUP}"
  cfn_export_outputs "${STACK_DATALAKE}"
  if [[ ! -d "${BUILD_DIR}/data" ]]; then
    log "generating sample data"
    python3 "${ROOT_DIR}/scripts/gen_data.py"
  else
    ok "sample data already generated (delete build/data to regenerate)"
  fi
  bash "${ROOT_DIR}/scripts/upload_data.sh"
}

step_roles() {
  cfn_deploy "${STACK_ROLES}" infra/02-roles.yaml \
    "DataBucketName=${DATA_BUCKET}" "GlueDatabaseName=${GLUE_DATABASE}" \
    "AnalyticsRoleName=${ANALYTICS_ROLE_NAME}" \
    "BusinessRoleName=${BUSINESS_ROLE_NAME}" \
    "RuntimeExecRoleName=${RUNTIME_EXEC_ROLE_NAME}" \
    "GatewayRoleName=${GATEWAY_ROLE_NAME}" \
    "TierDataPolicyName=${TIER_DATA_POLICY_NAME}" \
    "InterceptorFunctionName=${INTERCEPTOR_FUNCTION_NAME}" \
    "VerificationPrincipalArn=${LF_ADMIN_ARN}" \
    "ExternalClientSecretName=${OKTA_CLIENT_SECRET_NAME}"
  cfn_export_outputs "${STACK_ROLES}"
}

step_lakeformation() {
  out_require data_bucket analytics_role_arn business_role_arn
  # Must run before the grants: without stripping the inherited IAM_ALLOWED_PRINCIPALS
  # grant, every principal sees every column and the demo proves nothing.
  python3 "${ROOT_DIR}/scripts/lf_bootstrap.py" \
    --admin-arn "${LF_ADMIN_ARN}" --database "${GLUE_DATABASE}" --outputs "${OUTPUTS_FILE}"
  cfn_deploy "${STACK_LF}" infra/03-lakeformation.yaml \
    "DataBucketName=${DATA_BUCKET}" "GlueDatabaseName=${GLUE_DATABASE}" \
    "AnalyticsRoleArn=$(out_get analytics_role_arn)" \
    "BusinessRoleArn=$(out_get business_role_arn)"
  cfn_export_outputs "${STACK_LF}"
}

# --- the MCP server ------------------------------------------------------------

step_image() {
  cfn_deploy "${STACK_ECR}" infra/04-ecr.yaml "RepositoryName=${ECR_REPO}"
  cfn_export_outputs "${STACK_ECR}"
  bash "${ROOT_DIR}/scripts/build_push.sh"
}

# The interceptor is only a write guard here: it blocks write and delete tool calls. The
# tier is resolved by the server from the groups claim in the exchanged token, not by the
# interceptor.
step_interceptor() {
  cfn_deploy "${STACK_INTERCEPTOR}" infra/05-interceptor.yaml \
    "InterceptorRoleName=${INTERCEPTOR_ROLE_NAME}" \
    "InterceptorFunctionName=${INTERCEPTOR_FUNCTION_NAME}"
  cfn_export_outputs "${STACK_INTERCEPTOR}"
}

step_runtime() {
  out_require container_uri runtime_exec_role_arn analytics_role_arn business_role_arn
  [[ -n "${OKTA_ISSUER:-}" && -n "${OKTA_AUDIENCE:-}" ]] \
    || die "OKTA_ISSUER and OKTA_AUDIENCE must be set (see config.env)"
  cfn_deploy "${STACK_RUNTIME}" infra/06-runtime.yaml \
    "RuntimeName=${RUNTIME_NAME}" \
    "ContainerUri=$(out_get container_uri)" \
    "RuntimeExecRoleArn=$(out_get runtime_exec_role_arn)" \
    "OktaDiscoveryUrl=${OKTA_ISSUER%/}/.well-known/openid-configuration" \
    "OktaAudience=${OKTA_AUDIENCE}" \
    "OktaIssuer=${OKTA_ISSUER%/}" \
    "OktaGroupsClaim=${OKTA_GROUPS_CLAIM:-groups}" \
    "GlueDatabaseName=$(out_get glue_database)" \
    "AthenaWorkgroupName=$(out_get athena_workgroup)" \
    "AnalyticsRoleArn=$(out_get analytics_role_arn)" \
    "BusinessRoleArn=$(out_get business_role_arn)" \
    "ResourcePrefix=${PREFIX}"
  cfn_export_outputs "${STACK_RUNTIME}"
}

step_gateway() {
  out_require gateway_role_arn interceptor_function_arn
  cfn_deploy "${STACK_GATEWAY}" infra/07-gateway.yaml \
    "GatewayName=${GATEWAY_NAME}" \
    "RateLimitId=${RATE_LIMIT_ID}" \
    "GatewayRoleArn=$(out_get gateway_role_arn)" \
    "OktaDiscoveryUrl=${OKTA_ISSUER%/}/.well-known/openid-configuration" \
    "OktaAudience=${OKTA_AUDIENCE}" \
    "RequestsPerMinute=${REQUESTS_PER_MINUTE:-20}" \
    "InterceptorFunctionArn=$(out_get interceptor_function_arn)"
  cfn_export_outputs "${STACK_GATEWAY}"

  # Threaded through outputs.json so the verification scripts need no Okta env of their own.
  out_set okta_issuer "${OKTA_ISSUER%/}" \
          okta_audience "${OKTA_AUDIENCE}" \
          okta_spa_client_id "${OKTA_SPA_CLIENT_ID:-}" \
          okta_login_scope "${OKTA_LOGIN_SCOPE:-openid api}" \
          okta_redirect_uri "${OKTA_REDIRECT_URI:-http://localhost:8400/oauth/callback}"
}

step_target() {
  # Read the runtime and gateway from THEIR OWN STACKS, not from outputs.json.
  #
  # outputs.json is keyed by CloudFormation output name, so a second deployment of these
  # same templates under different names overwrites okta_runtime_arn and okta_gateway_id.
  # Trusting those keys here re-points whichever target runs next at the other
  # deployment's gateway - and because GatewayIdentifier cannot be changed in place,
  # CloudFormation replaces the target, silently moving it off its own gateway.
  local runtime_arn gateway_id endpoint
  runtime_arn="$(stack_output "${STACK_RUNTIME}" OktaRuntimeArn)"
  gateway_id="$(stack_output "${STACK_GATEWAY}" OktaGatewayId)"

  # The target needs the runtime's MCP endpoint, which embeds its URL-encoded ARN and
  # therefore only exists once the runtime has been created.
  endpoint="https://bedrock-agentcore.${AWS_REGION}.amazonaws.com/runtimes/$(
    python3 -c 'import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1], safe=""))' \
      "${runtime_arn}"
  )/invocations?qualifier=DEFAULT"
  out_set okta_runtime_mcp_endpoint "${endpoint}"

  # The credential provider is either created by this stack or adopted, and the choice is
  # declared in OKTA_OBO_PROVIDER_MODE rather than detected.
  #
  # Detecting it by probing for the name does not work, and failing that way is subtle: on
  # the second run the probe finds the provider the stack itself created, concludes it
  # should adopt, flips the condition off and CloudFormation deletes it - leaving a target
  # that still reports READY while pointing at a provider that no longer exists. The mode
  # has to come from configuration, because only configuration knows who owns the thing.
  local provider_args=()
  if [[ "${OKTA_OBO_PROVIDER_MODE:-create}" == "adopt" ]]; then
    local arn="${OKTA_OBO_PROVIDER_ARN:-arn:aws:bedrock-agentcore:${AWS_REGION}:${ACCOUNT_ID}:token-vault/default/oauth2credentialprovider/${OKTA_OBO_PROVIDER}}"
    log "adopting credential provider ${OKTA_OBO_PROVIDER} (not managed by this stack)"
    provider_args=("OboProviderArn=${arn}")
    out_set okta_obo_provider_arn "${arn}"
  else
    # OboProviderArn is cleared explicitly. "cloudformation deploy" reuses the previous
    # value of any parameter it is not given, so omitting it would silently keep an ARN
    # left over from an earlier adopt run and the provider would never be created.
    provider_args=(
      "OboProviderArn="
      "OboProviderName=${OKTA_OBO_PROVIDER}"
      "OktaDiscoveryUrl=${OKTA_ISSUER%/}/.well-known/openid-configuration"
      "OktaClientId=${OKTA_SERVICE_CLIENT_ID}"
      "ClientSecretName=${OKTA_CLIENT_SECRET_NAME}"
    )
    [[ -n "${OKTA_SERVICE_CLIENT_ID:-}" ]] \
      || die "set OKTA_SERVICE_CLIENT_ID (the Okta service app), or OKTA_OBO_PROVIDER_ARN to adopt an existing provider"
  fi

  cfn_deploy "${STACK_TARGET}" infra/08-gateway-target.yaml \
    "GatewayId=${gateway_id}" "TargetName=${GATEWAY_TARGET_NAME}" \
    "McpEndpoint=${endpoint}" \
    "ExchangeScope=${OKTA_EXCHANGE_SCOPE:-api}" \
    "ExchangeAudience=${OKTA_AUDIENCE}" \
    "${provider_args[@]}"
  cfn_export_outputs "${STACK_TARGET}"
}

step_kiro() { python3 "${ROOT_DIR}/scripts/write_kiro_config.py"; }

step_summary() {
  cat <<EOF

$(printf '=%.0s' {1..72})
  Athena MCP deployed
$(printf '=%.0s' {1..72})
  data bucket      $(out_get data_bucket)
  glue database    $(out_get glue_database)
  athena workgroup $(out_get athena_workgroup)
  runtime          $(out_get okta_runtime_arn)
  MCP URL          $(out_get okta_gateway_mcp_url)

  identity         Okta $(out_get okta_issuer)
                   group 'analytics' or 'business' selects the IAM role
  verify           python3 scripts/verify_permissions.py     (the gate: column diff per tier)
                   python3 scripts/probe_token.py            (login + on-behalf-of exchange)
                   python3 scripts/smoke_mcp.py --okta       (end to end through the gateway)
EOF
}

for step in "${STEPS[@]}"; do
  if want "${step}"; then
    printf '\n\033[1m--- %s ---\033[0m\n' "${step}"
    "step_${step}" || die "step '${step}' failed"
  fi
done
