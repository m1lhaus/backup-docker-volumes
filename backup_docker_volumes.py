#!/usr/bin/env python3
"""Back up Docker volumes, stopping only the compose projects that use them.

Usage: ./backup_docker_volumes.py <backup-destination-path> [options]

For every volume, find the running containers that mount it, resolve their
compose project, stop each project once, archive the volumes, then bring the
projects back up (compose handles dependency order, no containers recreated).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LABEL_PROJECT = "com.docker.compose.project"
LABEL_WORKDIR = "com.docker.compose.project.working_dir"
LABEL_CONFIG = "com.docker.compose.project.config_files"
LABEL_SERVICE = "com.docker.compose.service"

VERBOSE = False


class ComposeProjectNotFoundError(RuntimeError):
    """A container is not managed by a compose project."""


class NoMatchingVolumeError(RuntimeError):
    """An --include pattern matched no volume at all."""


def run(cmd: list[str], *, capture: bool = False) -> str:
    if VERBOSE:
        print("    $", " ".join(cmd))
    result = subprocess.run(cmd, text=True, capture_output=capture)
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd)}")
    return result.stdout if capture else ""


@dataclass
class Project:
    name: str
    workdir: str
    config_files: str
    services: set[str] = field(default_factory=set)

    def compose(self, *args: str) -> list[str]:
        cmd = ["docker", "compose", "-p", self.name, "--project-directory", self.workdir]
        for path in self.config_files.split(","):
            cmd += ["-f", path]
        return cmd + list(args)


def discover() -> tuple[list[str], list[dict]]:
    volumes = run(["docker", "volume", "ls", "-q"], capture=True).split()
    names = run(["docker", "ps", "-a", "--format", "{{.Names}}"], capture=True).split()
    containers = json.loads(run(["docker", "inspect", *names], capture=True)) if names else []
    return volumes, containers


def matches(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def plan(
    volumes: list[str],
    containers: list[dict],
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    partial_stop: bool = False,
) -> tuple[list[str], dict[str, Project]]:
    """Return the volumes to archive and the compose projects that must stop.

    `include`/`exclude` are fnmatch patterns; an empty `include` means "all".
    Raises NoMatchingVolumeError when an `include` pattern matches no volume,
    so a renamed volume cannot silently turn the backup into a no-op.

    With `partial_stop` only the services that mount a selected volume are
    stopped and restarted; otherwise the whole project is.
    """
    include = include or []
    exclude = exclude or []
    for pattern in include:
        if not any(matches(volume, [pattern]) for volume in volumes):
            raise NoMatchingVolumeError(f"--include pattern '{pattern}' matched no volume")
    volume_to_containers_map: dict[str, list[dict]] = {v: [] for v in volumes}
    for container in containers:
        for mount in container.get("Mounts") or []:
            if mount.get("Type") == "volume" and mount.get("Name") in volume_to_containers_map:
                volume_to_containers_map[mount["Name"]].append(container)

    targets: list[str] = []
    projects: dict[str, Project] = {}
    for volume in volumes:
        if include and not matches(volume, include):
            continue
        if matches(volume, exclude):
            continue
        targets.append(volume)
        for container in volume_to_containers_map[volume]:
            if not container["State"]["Running"]:
                continue
            labels = container["Config"].get("Labels") or {}
            try:
                project = Project(labels[LABEL_PROJECT], labels[LABEL_WORKDIR], labels[LABEL_CONFIG])
                service = labels[LABEL_SERVICE]
            except KeyError as exc:
                raise ComposeProjectNotFoundError(
                    f"container '{container['Name'].lstrip('/')}' has no compose label {exc}"
                ) from None
            project = projects.setdefault(project.name, project)  # reduce duplicates
            if partial_stop:
                project.services.add(service)

    if not partial_stop:
        # `compose stop` stops every service in the project, so restart all of them.
        for container in containers:
            labels = container["Config"].get("Labels") or {}
            project = projects.get(labels.get(LABEL_PROJECT, ""))
            if project and container["State"]["Running"]:
                project.services.add(labels[LABEL_SERVICE])
    return targets, projects


def backup_volume(volume: str, backup_dir: Path, timestamp: str) -> str:
    archive = f"{volume}__{timestamp}.tar.gz"
    run(
        [
            "docker", "run", "--rm",
            "-v", f"{volume}:/volume-data:ro",
            "-v", f"{backup_dir}:/backup",
            "alpine",
            "sh", "-c",
            f"tar czf '/backup/{archive}.part' -C /volume-data ."
            f" && chown 1000:1000 '/backup/{archive}.part'"
            f" && mv '/backup/{archive}.part' '/backup/{archive}'",
        ]
    )
    return archive


def main(argv: list[str] | None = None) -> int:
    global VERBOSE
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])  # type: ignore
    parser.add_argument("backup_dir", type=Path, help="destination directory for the archives")
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument("--verbose", "-v", action="store_true", help="echo every docker command")
    parser.add_argument(
        "--include", action="append", default=[], metavar="PATTERN",
        help="only back up volumes matching this fnmatch pattern (repeatable)",
    )
    parser.add_argument(
        "--exclude", action="append", default=[], metavar="PATTERN",
        help="skip volumes matching this fnmatch pattern (repeatable)",
    )
    parser.add_argument(
        "--partial-stop", action="store_true",
        help="stop only the services that mount a selected volume, not the whole project",
    )
    args = parser.parse_args(argv)
    VERBOSE = args.verbose

    backup_dir = args.backup_dir.expanduser().resolve()
    volumes, containers = discover()
    try:
        targets, projects = plan(
            volumes, containers, args.include, args.exclude, args.partial_stop
        )
    except (NoMatchingVolumeError, ComposeProjectNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"==> {len(targets)} volume(s) to back up, {len(projects)} compose project(s) to stop")
    for volume in targets:
        print(f"    {volume}")
    for project in projects.values():
        print(f"    stop {project.name} (services: {', '.join(sorted(project.services))})")

    if args.dry_run:
        print("==> Dry run, nothing changed.")
        return 0

    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y_%m_%d__%H_%M_%S")
    failures: list[str] = []
    stopped: list[Project] = []
    try:
        for project in projects.values():
            stopped.append(project)  # restart it even if stopping fails partway
            print(f"==> Stopping {project.name}...")
            run(project.compose("stop", *sorted(project.services)))

        for volume in targets:
            print(f"==> Backing up {volume}...")
            try:
                archive = backup_volume(volume, backup_dir, timestamp)
                print(f"    Done: {backup_dir / archive}")
            except RuntimeError as exc:
                print(f"    FAILED: {exc}", file=sys.stderr)
                failures.append(volume)
    finally:
        for project in reversed(stopped):
            print(f"==> Restarting {project.name}...")
            try:
                run(project.compose("up", "-d", "--no-recreate", *sorted(project.services)))
            except RuntimeError as exc:
                print(f"    FAILED to restart {project.name}: {exc}", file=sys.stderr)
                failures.append(project.name)

    if failures:
        print(f"==> Finished with {len(failures)} failure(s): {', '.join(failures)}", file=sys.stderr)
        return 1
    print(f"==> Backup complete. Files written to: {backup_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
