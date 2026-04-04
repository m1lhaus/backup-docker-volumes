# backup-docker-volumes

A Bash script that backs up all Docker volumes to compressed archives.

## What it does

1. Records all currently running containers
2. Stops them to ensure data consistency
3. Backs up every Docker volume to `<destination>/<volume>__<timestamp>.tar.gz`
4. Restarts containers — Tailscale sidecar containers (`ts-*`) first, then the rest

## Requirements

- Docker
- `alpine` image available (pulled automatically if not present)

## Usage

```bash
./backup_docker_volumes.sh <backup-destination-path>
```

**Example:**

```bash
./backup_docker_volumes.sh /mnt/backups/docker
```

The destination directory is created automatically if it does not exist. Each volume is saved as a separate `.tar.gz` file with a timestamp in the filename.

## Notes

- The script runs with `set -euo pipefail` and will abort on any error.
- Archives are owned by UID/GID `1000:1000`.
- Requires root or a user in the `docker` group.
