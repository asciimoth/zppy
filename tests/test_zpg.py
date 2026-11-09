"""Compatibility tests against zpg's independent packet validator."""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ZPG = shutil.which("zpg")


@unittest.skipUnless(ZPG, "zpg is not on PATH; use the Nix development shell")
class ZpgCompatibilityTests(unittest.TestCase):
    def run_worker(self, source):
        env = os.environ.copy()
        env.pop("ZIG_PROGRESS", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "progress.log"
            result = subprocess.run(
                [
                    ZPG,
                    "--test",
                    "--test-output",
                    str(log_file),
                    sys.executable,
                    "-c",
                    textwrap.dedent(source),
                ],
                env=env,
                capture_output=True,
                timeout=15,
            )
            log = log_file.read_text()
        self.assertEqual(result.returncode, 0, result.stderr.decode() + log)
        self.assertNotIn("error:", log)
        self.assertNotIn("no progress received", log)
        self.assertNotIn("incomplete", log)
        return result, log

    def test_nested_snapshots_and_output(self):
        result, log = self.run_worker("""
            import sys
            from zppy import Progress

            with Progress("build", 2, refresh_rate=None) as progress:
                root = progress.root
                assets = root.start("assets", 10)
                code = root.start("code")
                compile_task = code.start("compile", 4)
                assets.set_completed_items(3)
                compile_task.complete_one()
                assert progress.flush()
                assets.end()
                compile_task.set_name("link")
                assert progress.flush()
                compile_task.end()
                code.end()
            print("stdout unchanged")
            print("stderr unchanged", file=sys.stderr)
        """)
        self.assertEqual(result.stdout, b"stdout unchanged\n")
        self.assertEqual(result.stderr, b"stderr unchanged\n")
        for line in [
            'node=1 parent=0 completed=3 total=10 name="assets"',
            'node=3 parent=2 completed=1 total=4 name="compile"',
            'node=2 parent=1 completed=1 total=4 name="link"',
            'node=0 parent=255 completed=2 total=2 name="build"',
        ]:
            self.assertIn(line, log)

    def test_background_reports(self):
        _, log = self.run_worker("""
            import time
            from zppy import Progress

            with Progress("automatic", 2, initial_delay=0, refresh_rate=0.01) as p:
                p.root.complete_one()
                time.sleep(0.1)
                p.root.complete_one()
        """)
        self.assertIn('completed=2 total=2 name="automatic"', log)
        self.assertGreater(len(re.findall(r"snapshot=\d+ nodes=", log)), 1)

    def test_full_tree_and_reused_capacity(self):
        _, log = self.run_worker("""
            import time
            from zppy import Progress

            with Progress("capacity", refresh_rate=None) as p:
                tasks = [p.root.start("task-" + str(i), 1) for i in range(252)]
                p.root.start("overflow").end()
                # Let the reader drain any partial large packet.
                deadline = time.monotonic() + 5
                while not p.flush():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                for task in tasks:
                    task.end()
                with p.root.start("replacement", 2) as task:
                    task.complete_one()
                    assert p.flush()
        """)
        self.assertIn("nodes=253", log)
        self.assertIn('node=252 parent=0 completed=0 total=1 name="task-251"', log)
        self.assertIn('node=1 parent=0 completed=1 total=2 name="replacement"', log)
        self.assertIn('node=0 parent=255 completed=253 total=0 name="capacity"', log)
        self.assertNotIn('name="overflow"', log)

    def test_names_and_counter_boundaries(self):
        _, log = self.run_worker("""
            from zppy import Progress

            with Progress("x" * 121, 2**64, refresh_rate=None) as p:
                p.root.set_completed_items(2**64)
                p.root.increase_estimated_total_items(2**64)
                assert p.flush()
                p.root.set_name("é" * 61)
                p.root.set_estimated_total_items(0)
                assert p.flush()
                p.root.set_name(b"short\\0ignored")
        """)
        self.assertIn("completed=4294967295 total=4294967294", log)
        self.assertIn('name="' + "x" * 120 + '"', log)
        self.assertIn('total=0 name="short"', log)
        self.assertNotIn("ignored", log)


if __name__ == "__main__":
    unittest.main()
