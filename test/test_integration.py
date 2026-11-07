#!/usr/bin/env python3
"""Integration tests for backup_docker_volumes.py against real Docker.

Spins up the fixture compose projects in test/fixtures and is skipped when
Docker is unavailable.

Run: python3 -m unittest discover -s test -t . -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path

from test.helpers import ROOT

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCRIPT = ROOT / "backup_docker_volumes.py"

# volume name -> expected content of volume.txt
EXPECTED = {
    "test_volume_one": "compose1",
    "test_volume_two_a": "compose2-a",
    "test_volume_two_b": "compose2-b",
}


def docker_available() -> bool:
    """True when a working Docker daemon is reachable."""
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def compose(fixture: str, *args: str) -> None:
    """Run `docker compose` against one fixture project."""
    subprocess.run(
        ["docker", "compose", "-f", str(FIXTURES / fixture / "compose.yaml"), *args],
        check=True,
        capture_output=True,
    )


def volume_file(volume: str) -> str:
    """Read volume.txt from a volume, or "" when it does not exist yet."""
    result = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{volume}:/d:ro", "alpine", "cat", "/d/volume.txt"],
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def archive_contents(path: Path) -> dict[str, str]:
    """Map every regular file inside a .tar.gz to its decoded content."""
    with tarfile.open(path, "r:gz") as tar:
        return {
            member.name: tar.extractfile(member).read().decode()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }


@unittest.skipUnless(docker_available(), "Docker is not available")
class IntegrationTests(unittest.TestCase):
    """End-to-end runs against the fixture compose projects."""

    @classmethod
    def setUpClass(cls) -> None:
        """Start both fixture projects and wait until their volumes are written."""
        for fixture in ("compose1", "compose2"):
            compose(fixture, "down", "-v", "--remove-orphans")
            compose(fixture, "up", "-d")
        deadline = time.time() + 60
        for volume, expected in EXPECTED.items():
            while volume_file(volume) != expected:
                if time.time() > deadline:
                    raise AssertionError(f"fixture volume {volume} never got its content")
                time.sleep(0.5)

    @classmethod
    def tearDownClass(cls) -> None:
        """Remove the fixture projects, their containers and their volumes."""
        for fixture in ("compose1", "compose2"):
            compose(fixture, "down", "-v", "--remove-orphans")

    def setUp(self) -> None:
        """Give each test its own empty backup directory."""
        self._tmp = tempfile.TemporaryDirectory()
        self.backup_dir = Path(self._tmp.name) / "backups"

    def tearDown(self) -> None:
        """Delete the temporary backup directory."""
        self._tmp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the backup script against this test's backup directory."""
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(self.backup_dir), *args],
            capture_output=True,
            text=True,
        )

    def archives(self) -> list[str]:
        """Names of the archives written so far, sorted."""
        if not self.backup_dir.exists():
            return []
        return sorted(p.name for p in self.backup_dir.glob("*.tar.gz"))

    def test_zero_volumes(self) -> None:
        """An --include matching nothing exits 1 and writes no archive."""
        result = self.run_script("--include=test_volume_nonexistent_*")
        self.assertEqual(result.returncode, 1)
        self.assertIn("matched no volume", result.stderr)
        self.assertEqual(self.archives(), [])

    def test_one_volume(self) -> None:
        """Backing up a single volume yields one archive with the expected content."""
        result = self.run_script("--include=test_volume_one")
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = self.archives()
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].startswith("test_volume_one__"))
        contents = archive_contents(self.backup_dir / archives[0])
        self.assertEqual(contents["./volume.txt"].strip(), EXPECTED["test_volume_one"])

    def test_three_volumes(self) -> None:
        """Three volumes across two projects each produce a correct archive."""
        result = self.run_script("--include=test_volume*")
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = self.archives()
        self.assertEqual(len(archives), 3)
        for volume, expected in EXPECTED.items():
            matching = [a for a in archives if a.startswith(f"{volume}__")]
            self.assertEqual(len(matching), 1, f"missing archive for {volume}")
            contents = archive_contents(self.backup_dir / matching[0])
            self.assertEqual(contents["./volume.txt"].strip(), expected)

    def test_exclude_skips_matching_volumes(self) -> None:
        """--exclude removes matching volumes from an --include selection."""
        result = self.run_script("--include=test_volume*", "--exclude=test_volume_two_*")
        self.assertEqual(result.returncode, 0, result.stderr)
        archives = self.archives()
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].startswith("test_volume_one__"))

    def test_other_volumes_are_not_touched(self) -> None:
        """No archive is written for volumes outside the --include pattern."""
        result = self.run_script("--include=test_volume*")
        self.assertEqual(result.returncode, 0, result.stderr)
        for archive in self.archives():
            self.assertTrue(archive.startswith("test_volume"), f"unexpected archive {archive}")

    def test_containers_are_running_after_backup(self) -> None:
        """Every fixture container is running again once the backup finishes."""
        result = self.run_script("--include=test_volume*")
        self.assertEqual(result.returncode, 0, result.stderr)
        running = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True
        ).stdout.split()
        for name in (
            "test-compose1-writer",
            "test-compose2-writer-a",
            "test-compose2-writer-b",
        ):
            self.assertIn(name, running)


if __name__ == "__main__":
    unittest.main(verbosity=2)
