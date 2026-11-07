#!/usr/bin/env python3
"""Unit tests for backup_docker_volumes.py. No Docker required.

Run: python3 -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import contextlib
import io
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import ROOT, bdv


def make_container(
    name: str,
    volume: str,
    *,
    running: bool = True,
    project: str = "proj",
    service: str = "svc",
    labels: bool = True,
) -> dict:
    """Build a fake `docker inspect` entry for a container mounting one volume."""
    container = {
        "Name": f"/{name}",
        "State": {"Running": running},
        "Mounts": [{"Type": "volume", "Name": volume}],
        "Config": {"Labels": {}},
    }
    if labels:
        container["Config"]["Labels"] = {
            bdv.LABEL_PROJECT: project,
            bdv.LABEL_WORKDIR: f"/tmp/{project}",
            bdv.LABEL_CONFIG: f"/tmp/{project}/compose.yaml",
            bdv.LABEL_SERVICE: service,
        }
    return container


class PlanTests(unittest.TestCase):
    """Pure planning logic, no Docker involved."""

    def test_no_patterns_targets_every_volume(self) -> None:
        """Without --include/--exclude every volume is backed up."""
        targets, _ = bdv.plan(["a", "b"], [])
        self.assertEqual(targets, ["a", "b"])

    def test_include_filters_by_prefix(self) -> None:
        """--include=test_volume* keeps only volumes with that prefix."""
        targets, _ = bdv.plan(["test_volume_one", "other"], [], include=["test_volume*"])
        self.assertEqual(targets, ["test_volume_one"])

    def test_exclude_filters_by_prefix(self) -> None:
        """--exclude drops matching volumes and keeps the rest."""
        targets, _ = bdv.plan(
            ["test_volume_one", "test_volume_two_a"], [], exclude=["test_volume_two_*"]
        )
        self.assertEqual(targets, ["test_volume_one"])

    def test_include_and_exclude_combine(self) -> None:
        """--exclude wins over --include when both match the same volume."""
        targets, _ = bdv.plan(
            ["test_volume_one", "test_volume_two_a", "other"],
            [],
            include=["test_volume*"],
            exclude=["*_two_*"],
        )
        self.assertEqual(targets, ["test_volume_one"])

    def test_include_without_match_raises(self) -> None:
        """An --include pattern matching no volume fails instead of backing up nothing."""
        with self.assertRaises(bdv.NoMatchingVolumeError):
            bdv.plan(["a"], [], include=["nope*"])

    def test_include_matching_only_excluded_volumes_is_allowed(self) -> None:
        """A pattern that matches a volume is fine even if --exclude then drops it."""
        targets, projects = bdv.plan(["a"], [], include=["a"], exclude=["a"])
        self.assertEqual((targets, projects), ([], {}))

    def test_dangling_volume_is_targeted_without_stopping_anything(self) -> None:
        """A volume mounted by no container is still backed up, nothing stops."""
        targets, projects = bdv.plan(["orphan"], [])
        self.assertEqual(targets, ["orphan"])
        self.assertEqual(projects, {})

    def test_stopped_container_does_not_stop_project(self) -> None:
        """A volume used only by a stopped container is backed up without stopping."""
        containers = [make_container("c1", "vol1", running=False)]
        targets, projects = bdv.plan(["vol1"], containers)
        self.assertEqual(targets, ["vol1"])
        self.assertEqual(projects, {})

    def test_running_container_stops_its_project(self) -> None:
        """A running container using a volume causes its compose project to stop."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]
        _, projects = bdv.plan(["vol1"], containers)
        self.assertEqual(list(projects), ["p1"])
        self.assertEqual(projects["p1"].services, {"s1"})

    def test_projects_are_deduplicated(self) -> None:
        """Two volumes in the same project produce one stop entry, not two."""
        containers = [
            make_container("c1", "vol1", project="p1", service="s1"),
            make_container("c2", "vol2", project="p1", service="s2"),
        ]
        _, projects = bdv.plan(["vol1", "vol2"], containers)
        self.assertEqual(list(projects), ["p1"])
        self.assertEqual(projects["p1"].services, {"s1", "s2"})

    def test_restart_includes_services_that_mount_no_volume(self) -> None:
        """`compose stop` stops the whole project, so a volume-less service
        must still be restarted."""
        containers = [
            make_container("c1", "vol1", project="p1", service="s1"),
            make_container("watcher", "unrelated", project="p1", service="watcher"),
        ]
        _, projects = bdv.plan(["vol1"], containers)
        self.assertEqual(projects["p1"].services, {"s1", "watcher"})

    def test_missing_compose_labels_raises(self) -> None:
        """A running container without compose labels aborts the run."""
        containers = [make_container("c1", "vol1", labels=False)]
        with self.assertRaises(bdv.ComposeProjectNotFoundError):
            bdv.plan(["vol1"], containers)

    def test_untargeted_volume_without_labels_is_ignored(self) -> None:
        """A label-less container is not inspected when its volume is filtered out."""
        containers = [make_container("c1", "vol1", labels=False)]
        targets, projects = bdv.plan(["vol1"], containers, exclude=["vol1"])
        self.assertEqual((targets, projects), ([], {}))


class MainTests(unittest.TestCase):
    """main() control flow with Docker calls mocked out."""

    def test_backup_failure_still_restarts_project(self) -> None:
        """A failing archive still restarts the stopped project and exits 1."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            return ""

        with (
            mock.patch.object(bdv, "discover", return_value=(["vol1"], containers)),
            mock.patch.object(bdv, "run", side_effect=fake_run),
            mock.patch.object(bdv, "backup_volume", side_effect=RuntimeError("boom")),
            tempfile.TemporaryDirectory() as tmp,
        ):
            rc = bdv.main([tmp])

        self.assertEqual(rc, 1)
        self.assertTrue(any("stop" in cmd for cmd in calls), "project was not stopped")
        self.assertTrue(any("up" in cmd for cmd in calls), "project was not restarted")

    def test_dry_run_changes_nothing(self) -> None:
        """--dry-run runs no docker command and creates no backup directory."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]
        with (
            mock.patch.object(bdv, "discover", return_value=(["vol1"], containers)),
            mock.patch.object(bdv, "run") as run_mock,
            tempfile.TemporaryDirectory() as tmp,
        ):
            rc = bdv.main(["--dry-run", str(Path(tmp) / "out")])

        self.assertEqual(rc, 0)
        run_mock.assert_not_called()
        self.assertFalse((Path(tmp) / "out").exists())

    def test_unmatched_include_exits_nonzero(self) -> None:
        """An --include matching no volume exits 1 and stops nothing."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]
        with (
            mock.patch.object(bdv, "discover", return_value=(["vol1"], containers)),
            mock.patch.object(bdv, "run") as run_mock,
            tempfile.TemporaryDirectory() as tmp,
        ):
            rc = bdv.main(["--include=renamed_*", str(Path(tmp) / "out")])

        self.assertEqual(rc, 1)
        run_mock.assert_not_called()

    def test_successful_run_creates_archive_and_exits_zero(self) -> None:
        """A clean run creates the destination directory and exits 0."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]
        with (
            mock.patch.object(bdv, "discover", return_value=(["vol1"], containers)),
            mock.patch.object(bdv, "run"),
            mock.patch.object(bdv, "backup_volume", return_value="vol1__ts.tar.gz"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            out = Path(tmp) / "out"
            rc = bdv.main([str(out)])
            self.assertEqual(rc, 0)
            self.assertTrue(out.is_dir())

    def test_stop_failure_still_restarts_project(self) -> None:
        """A failing stop still restarts the project."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            calls.append(cmd)
            if "stop" in cmd:
                raise RuntimeError("stop boom")
            return ""

        with (
            mock.patch.object(bdv, "discover", return_value=(["vol1"], containers)),
            mock.patch.object(bdv, "run", side_effect=fake_run),
            mock.patch.object(bdv, "backup_volume", return_value="vol1__ts.tar.gz"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            with self.assertRaises(RuntimeError):
                bdv.main([tmp])

        self.assertTrue(any("up" in cmd for cmd in calls), "project was not restarted after failed stop")

    def test_restart_failure_is_reported(self) -> None:
        """A failing restart is reported and the run exits 1."""
        containers = [make_container("c1", "vol1", project="p1", service="s1")]

        def fake_run(cmd: list[str], *, capture: bool = False) -> str:
            if "up" in cmd:
                raise RuntimeError("restart boom")
            return ""

        with (
            mock.patch.object(bdv, "discover", return_value=(["vol1"], containers)),
            mock.patch.object(bdv, "run", side_effect=fake_run),
            mock.patch.object(bdv, "backup_volume", return_value="vol1__ts.tar.gz"),
            tempfile.TemporaryDirectory() as tmp,
        ):
            rc = bdv.main([tmp])

        self.assertEqual(rc, 1)


class RunTests(unittest.TestCase):
    """The thin subprocess wrapper."""

    def test_returns_stdout_when_capturing(self) -> None:
        """capture=True returns the command's stdout."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="hello\n")
        with mock.patch.object(bdv.subprocess, "run", return_value=completed):
            self.assertEqual(bdv.run(["docker", "ps"], capture=True), "hello\n")

    def test_returns_empty_string_without_capture(self) -> None:
        """capture=False returns an empty string."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ignored")
        with mock.patch.object(bdv.subprocess, "run", return_value=completed):
            self.assertEqual(bdv.run(["docker", "ps"]), "")

    def test_raises_on_nonzero_exit(self) -> None:
        """A non-zero exit code raises RuntimeError naming the command."""
        completed = subprocess.CompletedProcess(args=[], returncode=2, stdout="")
        with mock.patch.object(bdv.subprocess, "run", return_value=completed):
            with self.assertRaises(RuntimeError) as ctx:
                bdv.run(["docker", "bogus"])
        self.assertIn("docker bogus", str(ctx.exception))

    def test_prints_command_when_verbose(self) -> None:
        """--verbose echoes the command before running it."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        out = io.StringIO()
        with (
            mock.patch.object(bdv.subprocess, "run", return_value=completed),
            mock.patch.object(bdv, "VERBOSE", True),
            contextlib.redirect_stdout(out),
        ):
            bdv.run(["docker", "ps"])
        self.assertIn("$ docker ps", out.getvalue())


class ProjectTests(unittest.TestCase):
    """Compose command construction."""

    def test_compose_uses_single_config_file(self) -> None:
        """A single config file produces one -f flag."""
        project = bdv.Project("p1", "/work", "/work/compose.yaml")
        self.assertEqual(
            project.compose("stop"),
            [
                "docker", "compose", "-p", "p1", "--project-directory", "/work",
                "-f", "/work/compose.yaml", "stop",
            ],
        )

    def test_compose_splits_multiple_config_files(self) -> None:
        """Comma-separated config files each get their own -f flag."""
        project = bdv.Project("p1", "/work", "/work/a.yaml,/work/b.yaml")
        cmd = project.compose("up", "-d")
        self.assertEqual(cmd.count("-f"), 2)
        self.assertIn("/work/a.yaml", cmd)
        self.assertIn("/work/b.yaml", cmd)
        self.assertEqual(cmd[-2:], ["up", "-d"])


class BackupVolumeTests(unittest.TestCase):
    """Archive naming and the docker run invocation."""

    def test_returns_timestamped_archive_name(self) -> None:
        """The archive name is <volume>__<timestamp>.tar.gz."""
        with mock.patch.object(bdv, "run") as run_mock:
            archive = bdv.backup_volume("vol1", Path("/backups"), "2026_01_01__00_00_00")
        self.assertEqual(archive, "vol1__2026_01_01__00_00_00.tar.gz")

    def test_mounts_volume_read_only_and_destination(self) -> None:
        """The helper container mounts the volume read-only and the destination."""
        with mock.patch.object(bdv, "run") as run_mock:
            bdv.backup_volume("vol1", Path("/backups"), "ts")
        cmd = run_mock.call_args.args[0]
        self.assertIn("vol1:/volume-data:ro", cmd)
        self.assertIn("/backups:/backup", cmd)
        self.assertIn("alpine", cmd)


class MainGuardTests(unittest.TestCase):
    """The `if __name__ == "__main__"` entry point."""

    def test_main_guard_exits_with_main_status(self) -> None:
        """Running the file as a script exits with main()'s return code."""
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="")
        with (
            mock.patch.object(bdv.subprocess, "run", return_value=completed),
            mock.patch.object(sys, "argv", ["backup_docker_volumes.py", "/tmp/unused"]),
            tempfile.TemporaryDirectory() as tmp,
        ):
            sys.argv[1] = tmp
            with self.assertRaises(SystemExit) as ctx:
                runpy.run_path(str(ROOT / "backup_docker_volumes.py"), run_name="__main__")
        self.assertEqual(ctx.exception.code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
