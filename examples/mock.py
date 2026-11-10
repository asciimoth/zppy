"""Combine a native ``zpg --mock`` process and a forked Python worker."""

import os
import signal
import sys
import time
import traceback

from zppy import Node, Progress


def step(task: Node, label: str, delay: float = 0.2) -> None:
    """Show one short task and send a message while it is active."""
    with task.start(f"PY {label}", 1) as item:
        print(f"PY mock stdout: {label}")
        time.sleep(delay)
        item.complete_one()


def worker() -> None:
    with Progress("PY worker", 3, initial_delay=0) as progress:
        root = progress.root

        with root.start("PY discover", 2) as discover:
            step(discover, "read config")
            with discover.start("PY scan inputs", 3) as scan:
                for number in range(1, 4):
                    total = 2 if number == 2 else 1
                    with scan.start(f"PY input {number}", total) as item:
                        if number == 2:
                            step(item, "inspect metadata")
                        time.sleep(0.15)
                        item.complete_one()

        with root.start("PY build", 3) as build:
            # Keep all three branches visible while the build runs.
            with (
                build.start("PY assets", 2) as assets,
                build.start("PY code", 2) as code,
                build.start("PY verify", 2) as verify,
            ):
                for number in range(1, 3):
                    with assets.start(f"PY asset {number}", 2) as asset:
                        step(asset, "decode")
                        step(asset, "compress")
                assets.end()

                for number in range(1, 3):
                    with code.start(f"PY module {number}", 1) as module:
                        step(module, "compile", 0.25)
                code.end()

                for number in range(1, 3):
                    step(verify, f"check {number}", 0.25)
                verify.end()
            print("PY mock stderr: build complete", file=sys.stderr)

        with root.start("PY publish", 2) as publish:
            with publish.start("PY upload", 3) as upload:
                for number in range(1, 4):
                    print(f"PY mock stdout: upload chunk {number}")
                    time.sleep(0.15)
                    upload.complete_one()
            step(publish, "write manifest")

        print("PY mock stdout: complete")
        print("PY mock stderr: complete", file=sys.stderr)


def main() -> int:
    with Progress("demo", 2, initial_delay=0) as progress:
        native_task = progress.root.start("zpg --mock")
        python_task = progress.root.start("PY worker")
        native = native_task.spawn(["zpg", "--mock"])
        pid = None
        native_done = False
        python_done = False
        python_status = 0
        try:
            # Flush output before fork so buffered messages are not copied.
            sys.stdout.flush()
            sys.stderr.flush()
            pid = python_task.fork()
            if pid == 0:
                exit_code = 1
                try:
                    try:
                        worker()
                    finally:
                        sys.stdout.flush()
                        sys.stderr.flush()
                    exit_code = 0
                except BaseException:
                    traceback.print_exc()
                finally:
                    os._exit(exit_code)

            # Both workers run at once. Remove each task when its process ends.
            while not (native_done and python_done):
                if not native_done and native.poll() is not None:
                    native_done = True
                    native_task.end()
                if not python_done:
                    finished, python_status = os.waitpid(pid, os.WNOHANG)
                    if finished:
                        python_done = True
                        python_task.end()
                if not (native_done and python_done):
                    time.sleep(0.05)
        finally:
            if pid and not python_done:
                os.kill(pid, signal.SIGTERM)
                os.waitpid(pid, 0)
            if native.poll() is None:
                native.terminate()
            native.wait()
        return int(
            native.returncode != 0 or os.waitstatus_to_exitcode(python_status) != 0
        )


if __name__ == "__main__":
    raise SystemExit(main())
