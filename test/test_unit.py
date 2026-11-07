#!/usr/bin/env python3
"""Unit tests for backup_docker_volumes.py. No Docker required.

Run: python3 -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from test.helpers import bdv


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
