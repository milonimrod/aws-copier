#!/bin/sh
# Build the aws-copier image on this machine and export it as a tarball to copy onto
# the NAS, instead of building on the NAS itself (docker-compose.yml's `build: .` still
# works there too, if you'd rather build in place).
#
# Usage: docker/build-and-export.sh [amd64|arm64] [output-dir]
#   platform   defaults to this machine's native architecture (amd64 here)
#   output-dir defaults to the current directory
#
# Cross-building for arm64 from an amd64 machine requires QEMU emulation registered
# with buildx first (one-time, per Docker daemon restart):
#   docker run --privileged --rm tonistiige/binfmt --install all
set -e

PLATFORM="${1:-amd64}"
OUT_DIR="${2:-.}"
IMAGE="aws-copier:latest"
TARBALL="${OUT_DIR}/aws-copier-${PLATFORM}.tar.gz"

cd "$(dirname "$0")/.."

echo "Building ${IMAGE} for linux/${PLATFORM}..."
docker buildx build --platform "linux/${PLATFORM}" -t "${IMAGE}" --load .

echo "Exporting to ${TARBALL}..."
docker save "${IMAGE}" | gzip > "${TARBALL}"

echo "Done: ${TARBALL}"
echo "Copy it to your NAS, then run on the NAS:"
echo "  docker load -i $(basename "${TARBALL}")"
echo "  docker compose up -d"
