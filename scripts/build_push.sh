#!/usr/bin/env bash
# Build the MCP server image for ARM64 and push it to ECR.
# AgentCore Runtime runs arm64, so this always builds for linux/arm64 regardless of host.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

out_require ecr_repository_uri
REPO_URI="$(out_get ecr_repository_uri)"
TAG="${IMAGE_TAG:-$(date +%Y%m%d%H%M%S)}"
IMAGE="${REPO_URI}:${TAG}"

command -v docker >/dev/null || die "docker not found"
docker info >/dev/null 2>&1 || die "docker daemon is not running"

log "logging in to ECR"
aws_ ecr get-login-password | docker login --username AWS --password-stdin "${REPO_URI%%/*}" >/dev/null
ok "authenticated"

log "building ${IMAGE} (linux/arm64)"
docker buildx build \
  --platform linux/arm64 \
  --provenance=false \
  -t "${IMAGE}" \
  -t "${REPO_URI}:latest" \
  --push \
  "${ROOT_DIR}/server"
ok "pushed ${IMAGE}"

# The Runtime pins an immutable digest-free tag; using the unique tag forces a new
# Runtime version on every deploy instead of silently reusing a cached :latest.
out_set container_uri "${IMAGE}" image_tag "${TAG}"
ok "container_uri = ${IMAGE}"
