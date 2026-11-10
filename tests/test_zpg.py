"""Compatibility tests against zpg's independent packet validator."""

import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests.process_helpers import run_process

ZPG = shutil.which("zpg")


@unittest.skipUnless(ZPG, "zpg is not on PATH; use the Nix development shell")
class ZpgCompatibilityTests(unittest.TestCase):
    def run_worker(self, source):
        env = os.environ.copy()
        env.pop("ZIG_PROGRESS", None)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        with tempfile.TemporaryDirectory() as directory:
            log_file = Path(directory) / "progress.log"
            result = run_process(
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
                timeout=20,
                cwd=Path(__file__).resolve().parents[1],
            )
            log = log_file.read_text()
        self.assertEqual(result.returncode, 0, result.stderr.decode() + log)
        self.assertNotIn("error:", log)
        self.assertNotIn("no progress received", log)
        self.assertNotIn("incomplete", log)
        return result, log

    @unittest.skipUnless(hasattr(os, "fork"), "demo requires os.fork()")
    def test_demo_displays_both_trees_and_prefixes_all_python_entries(self):
        result, log = self.run_worker("""
            import runpy
            from zppy import Node

            # Check tasks at end(), too: a short final update can occur
            # between two automatic reports and be absent from the wire.
            end = Node.end
            def checked_end(node):
                progress = node._progress
                if progress is not None:
                    with progress._lock:
                        state = progress._nodes.get(node)
                        if state is not None and state.name.startswith(b'PY '):
                            assert state.completed == state.total, state
                end(node)
            Node.end = checked_end
            runpy.run_path('examples/mock.py', run_name='__main__')
        """)
        self.assertIn(b"PY mock stdout: complete", result.stdout)
        self.assertIn(b"PY mock stderr: complete", result.stderr)
        overlapping = False
        python_names = set()
        for frame in re.split(r"(?=snapshot=\d+ nodes=)", log):
            nodes = {
                int(index): (int(parent), name)
                for index, parent, name in re.findall(
                    r'node=(\d+) parent=(\d+) completed=\d+ total=\d+ name="([^"]*)"',
                    frame,
                )
            }
            roots = {name: index for index, (parent, name) in nodes.items()}
            if "PY worker" not in roots:
                continue
            worker = roots["PY worker"]
            descendants = set()
            for index, (_, name) in nodes.items():
                current = index
                seen = set()
                while current in nodes and current not in seen:
                    if current == worker:
                        self.assertTrue(name.startswith("PY "), name)
                        python_names.add(name)
                        descendants.add(index)
                        break
                    seen.add(current)
                    current = nodes[current][0]
            if "mock" in roots and len(descendants) > 1:
                native = roots["mock"]
                overlapping |= any(parent == native for parent, _ in nodes.values())
        self.assertTrue(overlapping, "native and Python descendants never overlapped")
        self.assertTrue({"PY discover", "PY build", "PY publish"} <= python_names)
        self.assertIn('node=0 parent=255 completed=2 total=2 name="demo"', log)

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

    def test_subprocess_trees_with_native_zpg_reports(self):
        _, log = self.run_worker("""
            import sys
            from zppy import Progress

            child_source = (
                'from zppy import Progress\\n'
                'with Progress("child", refresh_rate=None) as p:\\n'
                '    task = p.root.start("grandchild")\\n'
                '    proc = task.spawn(["zpg", "--report", "3", "8", '
                '"--name", "native leaf"])\\n'
                '    assert proc.wait(timeout=5) == 0\\n'
            )
            with Progress("parent", 2, refresh_rate=None) as p:
                first = p.root.start("first")
                second = p.root.start("second")
                a = first.spawn(["zpg", "--report", "2", "5", "--name", "native"])
                b = second.spawn([sys.executable, "-c", child_source])
                assert a.wait(timeout=5) == 0
                assert b.wait(timeout=5) == 0
                assert p.flush()
                first.end()
                assert p.flush()
                second.end()
        """)
        for line in [
            'node=1 parent=0 completed=2 total=5 name="native"',
            'node=2 parent=0 completed=0 total=0 name="child"',
            'node=3 parent=2 completed=3 total=8 name="native leaf"',
            'node=2 parent=1 completed=3 total=8 name="native leaf"',
            'node=0 parent=255 completed=2 total=2 name="parent"',
        ]:
            self.assertIn(line, log)

    @unittest.skipUnless(hasattr(os, "fork"), "requires os.fork()")
    def test_mixed_fork_and_spawn_tree(self):
        spawned_source = textwrap.dedent("""
            import os
            from zppy import Progress
            with Progress('spawned', refresh_rate=None) as p:
                pid = p.root.start('fork task').fork()
                if pid == 0:
                    with Progress('fork leaf', 10, refresh_rate=None) as c:
                        c.root.set_completed_items(6)
                    os._exit(0)
                assert os.waitpid(pid, 0)[1] == 0
        """)
        _, log = self.run_worker(f"""
            import os
            import sys
            from zppy import Progress

            with Progress('parent', 2, refresh_rate=None) as p:
                first = p.root.start('first')
                second = p.root.start('second')
                pid = first.fork()
                if pid == 0:
                    with Progress('forked', refresh_rate=None) as c:
                        native = c.root.start('native task')
                        proc = native.spawn([
                            'zpg', '--report', '3', '8', '--name', 'native leaf'
                        ])
                        assert proc.wait(timeout=5) == 0
                    os._exit(0)
                proc = second.spawn([sys.executable, '-c', {spawned_source!r}])
                assert proc.wait(timeout=5) == 0
                assert os.waitpid(pid, 0)[1] == 0
                assert p.flush()
                first.end()
                second.end()
        """)
        for line in [
            'node=1 parent=0 completed=0 total=0 name="forked"',
            'node=2 parent=0 completed=0 total=0 name="spawned"',
            'node=3 parent=1 completed=3 total=8 name="native leaf"',
            'node=4 parent=2 completed=6 total=10 name="fork leaf"',
            'node=0 parent=255 completed=2 total=2 name="parent"',
        ]:
            self.assertIn(line, log)

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
