"""Bound process tests and clean up their descendants after failures."""

import os
import signal
import subprocess


def run_process(args, *, timeout=15, **kwargs):
    with subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
        **kwargs,
    ) as process:
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        finally:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
