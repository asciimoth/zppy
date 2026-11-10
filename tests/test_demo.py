"""Exercise the demo's exit status and cleanup with real child processes."""

import os
import sys
import textwrap
import unittest
from pathlib import Path

from tests.process_helpers import run_process

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(hasattr(os, "fork"), "demo requires os.fork()")
class DemoLifecycleTests(unittest.TestCase):
    def run_demo(self, native_source, worker_source, body):
        source = """
import os
import signal
import sys
import time
from examples import mock as demo
from zppy import Node

spawn = Node.spawn
def native(self, args, **kwargs):
    assert args == ['zpg', '--mock'], args
    return spawn(self, [sys.executable, '-c', NATIVE_SOURCE], **kwargs)
Node.spawn = native

def no_children():
    try:
        pid, status = os.waitpid(-1, os.WNOHANG)
    except ChildProcessError:
        return
    raise AssertionError(('unreaped child', pid, status))
"""
        source += "\ndef worker():\n" + textwrap.indent(worker_source, "    ")
        source += "\ndemo.worker = worker\n" + textwrap.dedent(body)
        source = f"NATIVE_SOURCE = {native_source!r}\n" + source
        env = os.environ.copy()
        env.pop("ZIG_PROGRESS", None)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = run_process([sys.executable, "-c", source], env=env, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        return result

    def test_both_completion_orders_return_success_and_reap_children(self):
        for native, worker in (
            ("import time; time.sleep(0.15)", "pass"),
            ("pass", "time.sleep(0.15)"),
        ):
            with self.subTest(native=native):
                self.run_demo(native, worker, "assert demo.main() == 0\nno_children()")

    def test_native_nonzero_exit_returns_failure_and_reaps_children(self):
        self.run_demo(
            "raise SystemExit(7)",
            "time.sleep(0.05)",
            "assert demo.main() == 1\nno_children()",
        )

    def test_worker_exception_returns_failure_and_reaps_children(self):
        result = self.run_demo(
            "import time; time.sleep(0.1)",
            "print('before failure')\nraise RuntimeError('worker failed')",
            "assert demo.main() == 1\nno_children()",
        )
        self.assertIn(b"RuntimeError: worker failed", result.stderr)
        self.assertIn(b"before failure\n", result.stdout)

    def test_worker_signal_returns_failure_and_reaps_children(self):
        self.run_demo(
            "pass",
            "os.kill(os.getpid(), signal.SIGTERM)",
            "assert demo.main() == 1\nno_children()",
        )

    def test_worker_flush_failure_exits_without_running_parent_cleanup(self):
        result = self.run_demo(
            "import time; time.sleep(0.1)",
            """class BrokenOutput:
    def flush(self):
        raise BrokenPipeError('output closed')
sys.stdout = BrokenOutput()
""",
            "assert demo.main() == 1\nno_children()",
        )
        self.assertIn(b"BrokenPipeError: output closed", result.stderr)
        self.assertNotIn(b"AssertionError", result.stderr)
        self.assertNotIn(b"Exception ignored", result.stderr)

    def test_failed_fork_terminates_and_reaps_native_process(self):
        self.run_demo(
            "import time; time.sleep(30)",
            "pass",
            """
            def fail(self):
                raise OSError('fork failed')
            Node.fork = fail
            try:
                demo.main()
            except OSError as error:
                assert str(error) == 'fork failed'
            else:
                raise AssertionError('expected fork failure')
            no_children()
        """,
        )

    def test_interruption_terminates_and_reaps_both_children(self):
        self.run_demo(
            "import time; time.sleep(30)",
            "signal.pause()",
            """
            def interrupt(delay):
                raise KeyboardInterrupt
            demo.time.sleep = interrupt
            try:
                demo.main()
            except KeyboardInterrupt:
                pass
            else:
                raise AssertionError('expected interruption')
            no_children()
        """,
        )


if __name__ == "__main__":
    unittest.main()
