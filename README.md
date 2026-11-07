# backup-docker-volumes

Back up Docker volumes to compressed archives, stopping only the compose projects that
use the selected volumes.

## What it does

1. Lists all Docker volumes and inspects all containers.
2. Maps each volume to the containers that mount it, then to their compose project.
3. Stops each affected compose project once (`docker compose stop`).
4. Archives every selected volume to `<destination>/<volume>__<timestamp>.tar.gz`.
5. Restarts the stopped projects with `docker compose up -d --no-recreate`, so compose
   handles dependency order and no containers are recreated.

Volumes mounted by no container (dangling) or only by stopped containers are backed up
without stopping anything.

## Requirements

- Python 3.12 or newer (standard library only, no dependencies)
- Docker with the Compose v2 plugin (`docker compose`)
- The `alpine` image (pulled automatically if not present)
- Every running container that mounts a selected volume must belong to a compose project,
  otherwise the run aborts with `ComposeProjectNotFoundError`

## Usage

```bash
./backup_docker_volumes.py <backup-destination-path> [options]
```

**Example:**

```bash
./backup_docker_volumes.py /mnt/backups/docker
```

The destination directory is created automatically if it does not exist.

## Options

| Option | Description |
|---|---|
| `--include PATTERN` | Only back up volumes matching this `fnmatch` pattern. Repeatable. |
| `--exclude PATTERN` | Skip volumes matching this `fnmatch` pattern. Repeatable. `--exclude` wins over `--include`. |
| `--partial-stop` | Stop only the services that mount a selected volume, not the whole compose project. |
| `--dry-run` | Print the plan and exit without touching anything. |
| `--verbose`, `-v` | Echo every Docker command before running it. |

**Examples:**

```bash
# only volumes whose name starts with "windmill"
./backup_docker_volumes.py /mnt/backups/docker --include='windmill*'

# everything except rebuildable caches
./backup_docker_volumes.py /mnt/backups/docker --exclude='*_cache'

# preview what would happen
./backup_docker_volumes.py /mnt/backups/docker --include='windmill*' --dry-run

# stop only the services that actually mount the selected volumes
./backup_docker_volumes.py /mnt/backups/docker --include='windmill*' --partial-stop
```

### Stopping scope

By default a whole compose project is stopped, because `docker compose stop` without
service names stops every service in the project. With `--partial-stop` only the services
that mount a selected volume are stopped and restarted; the rest keep running. Either way
the restart uses `docker compose up -d --no-recreate <services>`, so compose still applies
the `depends_on` order from the compose file.

Services that share a network namespace (`network_mode: service:<sidecar>`) are fine as
long as the sidecar itself is not stopped: the restarted service joins the still-running
namespace and keeps its address. Only stopping the sidecar takes its dependants down.

## Safety behaviour

- **An `--include` pattern that matches no volume aborts the run** with exit code 1 and
  the message `--include pattern '<pattern>' matched no volume`. This prevents a renamed
  volume from silently turning the backup into a no-op.
- A pattern that matches a volume which `--exclude` then removes is *not* an error.
- If a backup fails, the stopped projects are still restarted (the restart runs in a
  `finally` block) and the script exits with code 1. The same applies to `SIGTERM`
  (`systemctl stop`, host shutdown, `kill`).
- Archives are written as `*.tar.gz.part` and renamed only on success, so a failed or
  partial archive never appears under its final name.
- Archives are owned by UID/GID `1000:1000`.

## Tests

The Python script has a test suite using the standard library `unittest` — no extra
dependencies.

```bash
python3 -m unittest discover -s test -t . -v   # all tests
python3 -m unittest test.test_unit             # unit tests only, no Docker needed
python3 -m unittest test.test_integration      # real Docker, can take up to few minutes
```

| File | Contents |
|---|---|
| `test/helpers.py` | `sys.path` bootstrap shared by both suites |
| `test/test_unit.py` | Mocked tests for planning, pattern matching and failure handling |
| `test/test_integration.py` | End-to-end runs against the fixture compose projects |
| `test/fixtures/compose1` | One named volume, `test_volume_one` |
| `test/fixtures/compose2` | Two named volumes, `test_volume_two_a` and `test_volume_two_b` |

Integration tests start the fixture projects, run the script with `--include=test_volume*`,
verify the archive contents, and remove the fixtures afterwards. They are skipped when
Docker is unavailable. Do not run them at the same time as a real backup on the same host.

