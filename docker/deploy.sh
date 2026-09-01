#!/bin/sh
# One-command deploy: build the image, export it directly onto the NAS's docker share
# (over the already-mounted NFS path), then SSH into the NAS to load it and restart the
# aws-copier container. Combines build-and-export.sh + the two manual NAS-side commands
# (docker load, docker compose up -d) into a single call from this machine.
#
# Usage: docker/deploy.sh [amd64|arm64]
#
# Requires passwordless SSH access to the NAS (ssh-copy-id once) and the NFS share
# already mounted locally (see README's Docker/NAS section).
#
# Override these via env vars if your setup differs:
#   NAS_HOST       - NAS hostname/IP for SSH (default: 192.168.8.201)
#   NAS_SSH_USER   - SSH username on the NAS (default: "Nimrod Milo" — yes, with a space)
#   NAS_LOCAL_DIR  - local NFS-mounted path to the NAS's aws-copier folder
#                    (default: /mnt/nas-docker/aws-copier)
#   NAS_REMOTE_DIR - the SAME folder's path as seen ON the NAS itself, used in the SSH
#                    command (default: /volume1/docker/aws-copier)
set -e

PLATFORM="${1:-amd64}"
NAS_HOST="${NAS_HOST:-192.168.8.201}"
NAS_SSH_USER="${NAS_SSH_USER:-Nimrod Milo}"
NAS_LOCAL_DIR="${NAS_LOCAL_DIR:-/mnt/nas-docker/aws-copier}"
NAS_REMOTE_DIR="${NAS_REMOTE_DIR:-/volume1/docker/aws-copier}"
TARBALL="aws-copier-${PLATFORM}.tar.gz"

cd "$(dirname "$0")/.."

./docker/build-and-export.sh "${PLATFORM}" "${NAS_LOCAL_DIR}"

echo "Loading image and restarting the container on ${NAS_HOST}..."
ssh -l "${NAS_SSH_USER}" "${NAS_HOST}" \
    "cd '${NAS_REMOTE_DIR}' && docker load -i '${TARBALL}' && docker compose up -d"

echo "Deploy complete."
