"""Show a changing task tree with the same phases as ``zpg --mock``."""

import sys
import time

from zppy import Node, Progress


def step(task: Node, label: str, delay: float = 0.2) -> None:
    """Show one short task and send a message while it is active."""
    with task.start(label, 1) as item:
        print(f"mock stdout: {label}")
        time.sleep(delay)
        item.complete_one()


def main() -> None:
    with Progress("mock", 3, initial_delay=0) as progress:
        root = progress.root

        with root.start("discover", 2) as discover:
            step(discover, "read config")
            with discover.start("scan inputs", 3) as scan:
                for number in range(1, 4):
                    with scan.start(f"input {number}", 1) as item:
                        if number == 2:
                            step(item, "inspect metadata")
                        time.sleep(0.15)
                        item.complete_one()

        with root.start("build", 3) as build:
            # Keep all three branches visible while the build runs.
            with (
                build.start("assets", 2) as assets,
                build.start("code", 2) as code,
                build.start("verify", 2) as verify,
            ):
                for number in range(1, 3):
                    with assets.start(f"asset {number}", 2) as asset:
                        step(asset, "decode")
                        step(asset, "compress")
                assets.end()

                for number in range(1, 3):
                    with code.start(f"module {number}", 1) as module:
                        step(module, "compile", 0.25)
                code.end()

                for number in range(1, 3):
                    step(verify, f"check {number}", 0.25)
                verify.end()
            print("mock stderr: build complete", file=sys.stderr)

        with root.start("publish", 2) as publish:
            with publish.start("upload", 3) as upload:
                for number in range(1, 4):
                    print(f"mock stdout: upload chunk {number}")
                    time.sleep(0.15)
                    upload.complete_one()
            step(publish, "write manifest")

        print("mock stdout: complete")
        print("mock stderr: complete", file=sys.stderr)


if __name__ == "__main__":
    main()
