#!/usr/bin/env bash
# Shared helpers: step runner, CloudFormation deploy wrapper, outputs.json threading.
set -euo pipefail

# Locate the repo root by walking up from $PWD looking for config.env.
# Shell-agnostic on purpose: BASH_SOURCE is not portable to zsh.
if [ -z "${ROOT_DIR:-}" ]; then
  _d="$PWD"
  while [ "${_d}" != "/" ] && [ ! -f "${_d}/config.env" ]; do _d="$(dirname "${_d}")"; done
  [ -f "${_d}/config.env" ] || { echo "cannot find config.env above $PWD" >&2; exit 1; }
  ROOT_DIR="${_d}"
  unset _d
fi
export ROOT_DIR
export BUILD_DIR="${ROOT_DIR}/build"
OUTPUTS_FILE="${BUILD_DIR}/outputs.json"
mkdir -p "${BUILD_DIR}"

# shellcheck disable=SC1091
source "${ROOT_DIR}/config.env"

[[ -f "${OUTPUTS_FILE}" ]] || echo '{}' > "${OUTPUTS_FILE}"

log()  { printf '\033[0;36m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[0;32m  ok\033[0m %s\n' "$*"; }
warn() { printf '\033[0;33m  !!\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[0;31mFAILED\033[0m %s\n' "$*" >&2; exit 1; }

aws_() { aws --profile "${AWS_PROFILE}" --region "${AWS_REGION}" "$@"; }

# outputs.json accessors --------------------------------------------------------

out_get() {
  python3 - "$1" <<'PY'
import json, os, sys
path = os.path.join(os.environ["BUILD_DIR"], "outputs.json")
with open(path) as fh:
    print(json.load(fh).get(sys.argv[1], ""))
PY
}

out_set() {
  # out_set KEY VALUE [KEY VALUE ...]  — atomic merge, never replaces the file
  python3 - "$@" <<'PY'
import json, os, sys, tempfile
path = os.path.join(os.environ["BUILD_DIR"], "outputs.json")
data = json.load(open(path)) if os.path.exists(path) else {}
args = sys.argv[1:]
data.update(dict(zip(args[::2], args[1::2])))
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
PY
}

out_require() {
  local key value
  for key in "$@"; do
    value="$(out_get "${key}")"
    [[ -n "${value}" ]] || die "outputs.json is missing '${key}' — run the earlier step first"
  done
}

# CloudFormation ---------------------------------------------------------------

# cfn_deploy STACK TEMPLATE [ParamKey=ParamValue ...]
cfn_deploy() {
  local stack="$1" template="$2"; shift 2
  log "deploying stack ${stack}"
  local args=(cloudformation deploy
    --stack-name "${stack}"
    --template-file "${ROOT_DIR}/${template}"
    --capabilities CAPABILITY_NAMED_IAM
    --no-fail-on-empty-changeset)
  if [[ $# -gt 0 ]]; then
    args+=(--parameter-overrides "$@")
  fi
  aws_ "${args[@]}" || die "stack ${stack} failed; see: aws cloudformation describe-stack-events --stack-name ${stack}"
  ok "stack ${stack}"
}

# cfn_export_outputs STACK  — copies every stack output into outputs.json,
# converting CamelCase output keys to snake_case.
cfn_export_outputs() {
  local stack="$1" json
  json="$(aws_ cloudformation describe-stacks --stack-name "${stack}" \
            --query 'Stacks[0].Outputs' --output json)"
  python3 - "${json}" <<'PY'
import json, os, re, sys, tempfile
path = os.path.join(os.environ["BUILD_DIR"], "outputs.json")
data = json.load(open(path)) if os.path.exists(path) else {}
for item in json.loads(sys.argv[1]) or []:
    key = re.sub(r"(?<!^)(?=[A-Z])", "_", item["OutputKey"]).lower()
    data[key] = item["OutputValue"]
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
with os.fdopen(fd, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
PY
}

# stack_output STACK OutputKey  — read one output straight from the stack.
#
# Use this instead of outputs.json whenever a step must refer to a specific deployment.
# outputs.json is keyed by CloudFormation output name, so two deployments of the same
# template share a key and the last one to run wins. Reading the stack keeps a step
# anchored to the deployment its own stack names identify.
stack_output() {
  local value
  value="$(aws_ cloudformation describe-stacks --stack-name "$1" \
             --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue" --output text 2>/dev/null)"
  [[ -n "${value}" && "${value}" != "None" ]] || die "stack $1 has no output $2 — deploy it first"
  printf '%s' "${value}"
}

stack_status() {
  aws_ cloudformation describe-stacks --stack-name "$1" \
    --query 'Stacks[0].StackStatus' --output text 2>/dev/null || echo "MISSING"
}

# Preflight -------------------------------------------------------------------

preflight() {
  log "preflight"
  local account
  account="$(aws_ sts get-caller-identity --query Account --output text)" \
    || die "cannot call STS with profile ${AWS_PROFILE}"
  # config.env derives ACCOUNT_ID from STS unless the operator pinned it, so only
  # assert when it was pinned -- otherwise this compares STS against itself.
  if [[ "${ACCOUNT_ID_SOURCE:-sts}" == "env" ]]; then
    [[ "${account}" == "${ACCOUNT_ID}" ]] \
      || die "wrong account: got ${account}, expected ${ACCOUNT_ID} (profile ${AWS_PROFILE})"
  fi
  command -v python3 >/dev/null || die "python3 not found"
  out_set account_id "${account}" region "${AWS_REGION}"
  ok "account ${account} / ${AWS_REGION} / profile ${AWS_PROFILE}"
  ok "lake formation admin ${LF_ADMIN_ARN}"
}
