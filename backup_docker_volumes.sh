#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# backup_docker_volumes.sh
# Usage: ./backup_docker_volumes.sh <backup-destination-path>
#
# Steps:
#   1. Record all currently running containers
#   2. Stop them
#   3. Back up every Docker volume to <backup-destination-path>/<volume>.tar.gz
#   4. Restart the previously running containers
# ---------------------------------------------------------------------------

if [[ $# -ne 1 ]]; then
    echo "Usage: $0 <backup-destination-path>" >&2
    exit 1
fi

BACKUP_DIR="$1"

if [[ ! -d "$BACKUP_DIR" ]]; then
    echo "Creating backup directory: $BACKUP_DIR"
    mkdir -p "$BACKUP_DIR"
fi

# ---------------------------------------------------------------------------
# 1. List & record running containers
# ---------------------------------------------------------------------------
echo "==> Listing running containers..."
mapfile -t RUNNING_CONTAINERS < <(docker ps --format '{{.Names}}')

if [[ ${#RUNNING_CONTAINERS[@]} -eq 0 ]]; then
    echo "    No running containers found."
else
    printf '    %s\n' "${RUNNING_CONTAINERS[@]}"
fi

# ---------------------------------------------------------------------------
# 2. Stop running containers
# ---------------------------------------------------------------------------
if [[ ${#RUNNING_CONTAINERS[@]} -gt 0 ]]; then
    echo "==> Stopping containers..."
    docker stop "${RUNNING_CONTAINERS[@]}"
fi

# ---------------------------------------------------------------------------
# 3. List all volumes & back each one up
# ---------------------------------------------------------------------------
echo "==> Listing all Docker volumes..."
mapfile -t VOLUMES < <(docker volume ls --quiet)

if [[ ${#VOLUMES[@]} -eq 0 ]]; then
    echo "    No Docker volumes found. Nothing to back up."
else
    printf '    %s\n' "${VOLUMES[@]}"
fi

TIMESTAMP=$(date +"%Y_%m_%d__%H_%M_%S")

for VOLUME in "${VOLUMES[@]}"; do
    ARCHIVE="${BACKUP_DIR}/${VOLUME}__${TIMESTAMP}.tar.gz"
    echo "==> Backing up volume '${VOLUME}' → ${ARCHIVE}"
    docker run --rm \
        -v "${VOLUME}:/volume-data:ro" \
        -v "${BACKUP_DIR}:/backup" \
        alpine \
        sh -c "tar czf '/backup/${VOLUME}__${TIMESTAMP}.tar.gz' -C /volume-data . && chown 1000:1000 '/backup/${VOLUME}__${TIMESTAMP}.tar.gz'"
    echo "    Done: ${ARCHIVE}"
done

# ---------------------------------------------------------------------------
# 4. Restart previously running containers
#    Phase 1: Tailscale sidecar containers (ts-*) first
#    Phase 2: All remaining containers after a short delay
# ---------------------------------------------------------------------------
if [[ ${#RUNNING_CONTAINERS[@]} -gt 0 ]]; then
    TS_CONTAINERS=()
    OTHER_CONTAINERS=()
    for C in "${RUNNING_CONTAINERS[@]}"; do
        if [[ "$C" == ts-* ]]; then
            TS_CONTAINERS+=("$C")
        else
            OTHER_CONTAINERS+=("$C")
        fi
    done

    if [[ ${#TS_CONTAINERS[@]} -gt 0 ]]; then
        echo "==> Restarting Tailscale containers (phase 1)..."
        docker start "${TS_CONTAINERS[@]}"
        echo "    Waiting 5 seconds for Tailscale networks to become ready..."
        sleep 5
    fi

    if [[ ${#OTHER_CONTAINERS[@]} -gt 0 ]]; then
        echo "==> Restarting remaining containers (phase 2)..."
        docker start "${OTHER_CONTAINERS[@]}"
    fi
fi

echo "==> Backup complete. Files written to: ${BACKUP_DIR}"
