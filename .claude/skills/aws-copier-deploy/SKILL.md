---
name: aws-copier-deploy
description: This skill should be used when deploying aws-copier changes to the NAS — building the Docker image, pushing it to the NAS, or restarting the container. Also use it when troubleshooting a deploy failure ("pull access denied", "permission denied ... docker.sock", "Dockerfile: no such file or directory"), when setting up auto-deployment on a fresh machine, or when asked "how do I deploy this" / "redeploy" / "ship this change".
version: 1.0.0
---

# AWS Copier Deploy

aws-copier runs as a Docker container on the user's UGreen NAS. The dev machine builds the
image and deploys it to the NAS over SSH — there is no CI/CD, no registry, no build-on-NAS
in normal use.

## One-command deploy (normal path)

```bash
./docker/deploy.sh amd64
```

This chains `docker/build-and-export.sh` (build the image, drop the tarball straight onto
the NAS's docker share over the mounted NFS path) with an SSH call to the NAS that runs
`docker load` + `docker compose up -d`. One command, no manual steps, on a properly set-up
machine.

**Do this after any code change before considering the change "shipped"** — pushing to git
does not deploy anything; the NAS pulls nothing on its own.

Always verify after deploying:

```bash
ssh -l "Nimrod Milo" 192.168.8.201 "docker ps --filter name=aws-copier --format 'table {{.Names}}\t{{.Status}}' && docker logs aws-copier --tail 15"
```

Look for a clean startup (`S3Manager initialized`, `AWS credentials loaded from: config.yaml`,
folders being processed) and no tracebacks in the tail.

## Prerequisites checklist for auto-deployment to work

If `deploy.sh` fails, walk this list — it's almost always one of these, in this order:

1. **NAS docker share mounted locally over NFS.** `docker/build-and-export.sh` writes the
   tarball straight to this path — `/mnt/nas-docker/aws-copier` by default (`NAS_LOCAL_DIR`
   env var to override). Check with `ls /mnt/nas-docker/aws-copier`. If missing, the NFS
   mount itself needs setting up first (outside this skill's scope — that's host-level
   `/etc/fstab` or systemd automount config, not an aws-copier concern).
2. **This machine builds the same architecture as the NAS.** Confirmed for this setup:
   both `amd64`. If unsure, `ssh <user>@<nas-ip> uname -m` — `x86_64` → `amd64`,
   `aarch64` → `arm64`. Cross-building the other architecture from this machine needs
   QEMU emulation registered with buildx first: `docker run --privileged --rm
   tonistiige/binfmt --install all`.
3. **Passwordless SSH key access to the NAS.** `ssh-copy-id -l "Nimrod Milo" 192.168.8.201`
   (or whatever `NAS_SSH_USER`/`NAS_HOST` are) — one-time, needs the NAS password
   interactively. Test with `ssh -o BatchMode=yes -l "Nimrod Milo" 192.168.8.201 echo ok`;
   if that hangs or asks for a password, this step isn't done.
4. **The SSH user is in the NAS's `docker` group.** Without this, `docker load`/
   `docker compose` fail with `permission denied while trying to connect to the docker API
   at unix:///var/run/docker.sock`. Fix: `sudo usermod -aG docker "Nimrod Milo"` **on the
   NAS**, run interactively (needs the NAS sudo password) — then open a **new** SSH session,
   since group membership only applies to fresh logins. Verify: `ssh -l "Nimrod Milo"
   192.168.8.201 groups` should list `docker`.
5. **The NAS-side `docker-compose.yaml` has no `build:` directive.** The deploy folder
   (`/volume1/docker/aws-copier` on the NAS) holds only the compose file, `config.yaml`, and
   the image tarball — no source, no `Dockerfile`. If `build:` is present there and the
   `image:` tag isn't already loaded, `docker compose up -d` tries to pull `aws-copier:latest`
   from a registry (fails — it's not published anywhere) and then falls back to building,
   which fails with `failed to read dockerfile: open Dockerfile: no such file or directory`.
   The repo's own `docker-compose.yml` (for people building from a full checkout) keeps
   `build: .` — that one's fine; only the NAS-deployed copy must omit it.
6. **`config.yaml` is present and current in the NAS deploy folder.** It's gitignored (holds
   real AWS credentials) so it's never part of the image or the git-tracked
   `docker-compose.yaml`/repo — it's copied over by hand, independently, whenever it changes.
   If you edit the repo's local `config.yaml` for a config change, remember to also
   `cp config.yaml /mnt/nas-docker/aws-copier/config.yaml` — `deploy.sh` does not do this
   for you, only `docker-compose.yaml` changes need a manual copy in the same way.

## Environment variable overrides

`deploy.sh` and `build-and-export.sh` assume this machine's actual setup as defaults.
Override via env vars if any of these differ:

| Var | Default | Meaning |
|---|---|---|
| `NAS_HOST` | `192.168.8.201` | NAS IP/hostname for SSH |
| `NAS_SSH_USER` | `Nimrod Milo` (yes, with a space — use `-l "Nimrod Milo"`, not `user@host` syntax) | SSH login user on the NAS |
| `NAS_LOCAL_DIR` | `/mnt/nas-docker/aws-copier` | Local NFS-mounted path to the NAS's aws-copier folder |
| `NAS_REMOTE_DIR` | `/volume1/docker/aws-copier` | The same folder's path as seen on the NAS itself (used in the SSH command) |

## Manual fallback (no SSH access set up)

```bash
docker/build-and-export.sh amd64   # writes aws-copier-amd64.tar.gz to the current dir
# copy the tarball to the NAS (shared folder, scp, etc.)
```
Then on the NAS itself:
```bash
docker load -i aws-copier-amd64.tar.gz
docker compose up -d   # no --build — uses the image just loaded
```

## What deploying does NOT do

- Does not touch `config.yaml` or `docker-compose.yaml` on the NAS — those are copied
  independently, only when they've actually changed.
- Does not restart cleanly mid-scan without cost: recreating the container restarts
  `scan_all_folders()` from the top. Already-synced folders skip fast (mtime cache in
  `.milo_backup.info`), but for a large library this still means the real-time watcher
  (which only starts after the full scan completes) is unavailable again for a while after
  every redeploy. See the `aws-copier-monitoring` skill for how to check scan progress.
