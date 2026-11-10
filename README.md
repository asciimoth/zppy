# zppy

Python reporter for the Zig progress protocol. It sends task trees to a parent
process through the `ZIG_PROGRESS` pipe. Use a receiver such as
[`zpg`](https://github.com/asciimoth/zpg) to display progress.

Requires Python 3.11 or later. The library has no runtime dependencies.
The pipe transport supports POSIX systems, including Linux and macOS.

## Usage

Run the example under `zpg` from the Nix development shell:

```sh
just run
```

The example runs `zpg --mock` as a subprocess and a Python worker as a forked
process at the same time. Both trees appear under `demo`. All Python worker
progress labels start with `PY`. Both workers also write messages to stdout
and stderr.

```python
import time

from zppy import Progress

with Progress("build", estimated_total_items=2) as progress:
    for name in ("assets", "code"):
        with progress.root.start(name, estimated_total_items=10) as task:
            for _ in range(10):
                time.sleep(0.1)  # Replace this with one item of work.
                task.complete_one()
```

Each child can start more children. When a child ends, it disappears from the
tree and adds one completed item to its parent. Do not also call
`parent.complete_one()` for the same child. A context manager ends its task on
normal exit and on exceptions. It does not suppress exceptions or report task
success or failure.

Without a valid `ZIG_PROGRESS` channel, reporting is disabled. Task operations
still work as no-ops. The library does not print to stdout or stderr, and does
not draw its own terminal display.

## Child processes

Use `Node.spawn()` to start a process with its own progress pipe. It accepts
the arguments to `subprocess.Popen` and returns a `Popen` object:

```python
import sys

from zppy import Progress

with Progress("build", estimated_total_items=1) as progress:
    with progress.root.start("compile") as task:
        with task.spawn([sys.executable, "worker.py"]) as process:
            returncode = process.wait()
```

In `worker.py`, use `Progress` as usual. The parent receives the child's tree
and sends it upstream with its own tasks. A child can use `Node.spawn()` to
start more processes. Use a separate task node for each process; processes on
different nodes can run at the same time.

This follows [Zig's child progress behavior][zig-child]: the child's root
replaces the task node in each report. Its descendants appear below that node.
If the child root has no name, the task keeps its local name. Until the first
complete report arrives, the task keeps its local state. The last complete
tree stays visible after the child closes its pipe or exits.

Wait for the process, then end its task, as in the example above. Process exit
does not end the task or mark success or failure. Ending the task completes one
item in its parent. Ending a task or closing the reporter closes its child
progress pipes; it does not wait for or terminate processes. Use one process per
active node. A second `spawn()` or `fork()` on the same node raises `ValueError`.

The update thread collects child reports. With `refresh_rate=None`, call
`flush()` regularly while children run. It collects their reports even if the
upstream pipe is full. Collection is nonblocking, and work per child per update
is bounded. Partial packets wait for more bytes. Invalid packets close only
that child's channel; its last valid tree remains available.

`spawn()` copies the child environment and sets `ZIG_PROGRESS` to the new pipe.
It preserves other environment entries and adds the pipe to `pass_fds`.
`close_fds` must be true. It does not change the parent environment. Disabled,
ended, or closed nodes still start processes, but remove `ZIG_PROGRESS` from
their environment. Do not pass the upstream progress descriptor to children.
All processes in the tree must use the same `name_size`; there is no format
negotiation.

[zig-child]: https://github.com/ziglang/zig/blob/0.15.2/lib/std/process/Child.zig#L96-L104

## Forked processes

On systems with `os.fork()`, use `Node.fork()` to attach a forked process to a
task. It returns the child's PID in the parent and `0` in the child:

```python
import os

from zppy import Progress

with Progress("parent", estimated_total_items=1) as progress:
    with progress.root.start("worker") as task:
        pid = task.fork()
        if pid == 0:
            with Progress("worker", estimated_total_items=10) as child:
                for _ in range(10):
                    child.root.complete_one()
            os._exit(0)
        _, status = os.waitpid(pid, 0)
```

The child gets a private `ZIG_PROGRESS` pipe. Create a fresh `Progress` in the
child, with the same `name_size` as its parent. All inherited reporters and
their nodes are disabled in the child. Their owned pipe descriptors are closed,
their pending packets are discarded, and their locks and thread state are
reset. Caller-owned descriptors remain the caller's responsibility. The
parent's reporters and environment stay unchanged.

The forked tree replaces its task node, exactly as for `spawn()`. Forked
children can use either `fork()` or `spawn()` to create more children. Wait for
the child with `os.waitpid()`, then end its task in the parent. Close the child's
reporter before `os._exit()`, which skips normal cleanup. Handle child errors
and exit status as required by your application.

Forks on disabled, ended, or closed nodes still run, but the child has no
`ZIG_PROGRESS` channel. A failed fork closes the new pipe and leaves the parent
reporter usable. A fork channel's source descriptor stays open in its child
until process exit; a nested `Node.fork()` closes its inherited copy in the
grandchild. These descriptors are not inherited across exec.

Use `Node.fork()` for this connection. Direct `os.fork()` calls and
`multiprocessing` do not set it up. This method protects zppy's own state;
application code and other libraries must also meet the platform's
[fork requirements](https://docs.python.org/3/library/os.html#os.fork).

## API

`Progress(root_name="", estimated_total_items=0, *, refresh_rate=0.08,
initial_delay=0.2, fd=None, disable=False, name_size=120)` creates a reporter.
Use `progress.root` to access its root task.

| Option | Meaning |
| --- | --- |
| `root_name` | Root label, as `str` or `bytes`. |
| `estimated_total_items` | Estimated root item count. Zero means unknown. |
| `refresh_rate` | Seconds between reports; `None` for manual reports. |
| `initial_delay` | Seconds before the first automatic report. |
| `fd` | Explicit pipe descriptor. `None` reads `ZIG_PROGRESS`. |
| `disable` | Disable all reporting when true. |
| `name_size` | Name field size: `120` by default, or `40` for the old format. |

Timer values must be finite numbers no greater than `threading.TIMEOUT_MAX`.
`refresh_rate` must be positive, and `initial_delay` must be nonnegative.

`progress.enabled` indicates whether the channel is open.
`progress.flush()` tries to send a snapshot immediately. It returns `True` if
the current snapshot was sent in full, or `False` if reporting is disabled or
the write could not finish. It does not wait for pipe space.

`progress.close()` stops the update thread, tries to send the final state, and
releases its descriptor. Use a context manager or call `close()` explicitly.
Closing does not force counts to their totals or remove the final tree from the
receiver. Ending the root also closes the reporter.

Nodes provide these methods:

| Method | Action |
| --- | --- |
| `start(name, estimated_total_items=0)` | Create a child task. |
| `spawn(args, **kwargs)` | Start a process and collect its progress. |
| `fork()` | Fork and collect the child's progress at this node. |
| `complete_one()` | Add one completed item. |
| `set_completed_items(count)` | Set the absolute completed count. |
| `set_estimated_total_items(count)` | Set the estimated total. |
| `increase_estimated_total_items(count)` | Add to the estimated total. |
| `set_name(name)` | Change the task label. |
| `end()` | Remove this branch and complete one item in its parent. |

Calls are thread-safe. Repeated `end()` calls have no effect. Task updates on
ended nodes also have no effect; `spawn()` and `fork()` still start a process as
described above. Invalid arguments raise `TypeError` or
`ValueError`, including when reporting is disabled.

Counts must be nonnegative integers. Completed counts are limited to
`4294967295`; totals are limited to `4294967294`, because Zig reserves the next
value. Larger values stop at these limits. Completed counts can exceed totals.

Names are zero-padded to the field size. Text is encoded as UTF-8 and shortened
at a character boundary. Byte names are shortened without decoding. Bytes after
the first NUL are ignored. The reporter tracks up to 253 active nodes, including
the root. Additional nodes are no-ops. Ending nodes makes space for new nodes.
Combined reports also have a 253-node limit. Local nodes take priority, followed
by child trees in process creation order. Child roots replace local nodes
without using more slots. Excess descendants are omitted with their branches;
they can appear in later reports when space is available.

## Protocol compatibility

The default uses a 120-byte name field, as used by the Zig 0.17 standard library
and the `zpg` version pinned in this project. Each node has two little-endian
32-bit counters and 120 name bytes. A packet contains the node count, all node
records, then all parent indices. The root's parent is `255`.

The [published specification][protocol] uses **40-byte** names. Select that
format explicitly with `Progress(name_size=40)`. The two formats are not wire
compatible. Both use `ZIG_PROGRESS`, and there is no version handshake. Select
the format that matches your receiver.

[protocol]: https://andrewkelley.me/post/zig-new-cli-progress-bar-explained.html#the-zig-progress-protocol-specification

The channel can have multiple writers. Each packet replaces the previous tree;
it does not add tasks to it. For example, if `zppy` sends a tree and then
`zpg --report` writes to the same pipe, the receiver shows the single-node
`zpg --report` snapshot until `zppy` writes again. Separate writes from
different processes can also interleave when a `zppy` packet is larger than one
atomic pipe write. Use `Node.spawn()` or `Node.fork()` to combine progress from
separate processes through private pipes. Create a fresh reporter in each child.

The reporter accepts only writable POSIX pipes. Other descriptors, including
regular files and read-only pipes, disable reporting without changing their
data or blocking mode. Reporting is disabled on other platforms.

The reporter duplicates the supplied descriptor and never closes the original.
Its duplicate is not inherited across exec. It sets the channel to nonblocking
mode; this flag also applies to the original descriptor. It does not change
`ZIG_PROGRESS` in the environment when constructed. `Node.fork()` sets it in the
forked child only.

A full pipe can delay or drop updates. Partial packets resume before a new
snapshot is sent. A permanent write error disables reporting. Closing never
waits for pipe space: a final report can be lost, or a large partial packet can
remain incomplete if the receiver stops reading. If final delivery is required,
retry `flush()` while the receiver is active before closing.

## Tests

Run `just check` in the Nix development shell for tests, lint, spelling, and
format checks. Use `just test` to run only the tests.

The suite covers packet fragmentation, full pipes, invalid reports, node
limits, descriptor cleanup, concurrent spawning and forking, nested process
trees, and child failures. Random tree tests use a fixed seed. Process tests
use timeouts, and isolated test processes clean up their descendants on failure.

With `zpg` on `PATH`, the suite also checks protocol compatibility and runs the
full demo. It checks that the native and Python trees appear at the same time,
that Python labels have the `PY` prefix, and that both tasks complete. These
integration tests are skipped when `zpg` is absent; fork tests are skipped on
systems without `os.fork()`. The full demo test takes several seconds.
