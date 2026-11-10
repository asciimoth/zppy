# zppy

`zppy` is a Python library that reports task progress through the Zig progress
protocol. Use it to report tasks, subtasks, and progress from child processes.

`zppy` requires Python 3.11 or later. The library has no runtime dependencies.
The pipe transport supports POSIX systems, including Linux and macOS.

## Zig progress protocol

The Zig progress protocol lets a process send a tree of tasks to its parent
through a pipe. Each task has a name, a completed item count, and an estimated
total. The `ZIG_PROGRESS` environment variable identifies the pipe. The parent
can combine progress from child processes and display it in one place.

You can use the protocol outside the Zig toolchain. For example,
[`zpg`](https://github.com/asciimoth/zpg) runs commands and displays their
progress in a terminal. With `zppy`, Python programs can send progress to
`zpg` or another compatible receiver.

See Andrew Kelley's [Zig progress protocol specification][protocol] for the
protocol design.

![zpg displaying a task tree](https://raw.githubusercontent.com/asciimoth/zpg/master/demo.gif)

## Library usage

If you use this library, [open an issue][issues] or [send a pull
request][pulls] with a link to your project. This helps us track use of the
Zig progress protocol outside the Zig toolchain.

Add the [`zig-progress-protocol` topic][topic] to your GitHub repository. Use
the same tag on other platforms so people can find related projects.

[issues]: https://github.com/asciimoth/zppy/issues
[pulls]: https://github.com/asciimoth/zppy/pulls
[topic]: https://github.com/topics/zig-progress-protocol

### Report tasks

Save this example as `build.py`:

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

Run `build.py` with a progress display:

```sh
zpg python build.py
```

Each child task can start more child tasks. When a child task ends, it leaves
the tree and adds one completed item to its parent task. Do not also call
`parent.complete_one()` for the same child task.

A context manager ends its task on normal exit and on exceptions. The context
manager does not suppress exceptions or report task success or failure.

Without a valid `ZIG_PROGRESS` channel, reporting is disabled. Task operations
have no effect. The library does not print to stdout or stderr. It does not
draw its own terminal display.

### Run the combined example

From the repository root, run:

```sh
PYTHONPATH=src zpg python -u examples/mock.py
```

This example requires `zpg` on `PATH` and a system with `os.fork()`. It runs
`zpg --mock` as a subprocess and a Python worker as a forked process at the
same time. Both task trees appear under `demo`. Python task labels start with
`PY`. Both workers also write messages to stdout and stderr.

### Child processes

Use `Node.spawn()` to start a process with its own progress pipe.
`Node.spawn()` accepts the arguments to `subprocess.Popen` and returns a
`Popen` object:

```python
import sys

from zppy import Progress

with Progress("build", estimated_total_items=1) as progress:
    with progress.root.start("compile") as task:
        with task.spawn([sys.executable, "worker.py"]) as process:
            returncode = process.wait()
```

In `worker.py`, use `Progress`. The parent process combines the child's task
tree with its own tasks and sends the combined tree upstream. A child process
can use `Node.spawn()` to start more processes. Use a separate task node for
each process. Processes on different nodes can run at the same time.

`Node.spawn()` follows [Zig's child progress behavior][zig-child]. The child's
root replaces the task node in each report. The child root's descendants
appear below that node. If the child root has no name, the task keeps its
local name. Until the first complete report arrives, the task keeps its local
state. The last complete tree stays visible after the child closes its pipe or
exits.

Wait for the process, then end its task, as in the example above. Process exit
does not end the task or mark success or failure. Ending the task completes
one item in its parent.

Ending a task or closing the reporter closes its child progress pipes. Neither
operation waits for or terminates processes. Use one process per active node.
A second `spawn()` or `fork()` on the same node raises `ValueError`.

The update thread collects child reports. With `refresh_rate=None`, call
`flush()` regularly while children run.

`spawn()` copies the child environment and sets `ZIG_PROGRESS` to the new
pipe. It preserves other environment entries and adds the pipe to `pass_fds`.
`close_fds` must be true. `spawn()` does not change the parent environment.
Disabled, ended, or closed nodes still start processes, but remove
`ZIG_PROGRESS` from the child environment. Do not pass the upstream progress
descriptor to children.

All processes in the tree must use the same `name_size`. There is no format
negotiation.

[zig-child]: https://github.com/ziglang/zig/blob/0.15.2/lib/std/process/Child.zig#L96-L104

### Forked processes

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

The child gets a private `ZIG_PROGRESS` pipe. Create a new `Progress` in the
child, with the same `name_size` as its parent. All inherited reporters and
their nodes are disabled in the child. Caller-owned descriptors remain the
caller's responsibility. The parent's reporters and environment stay
unchanged.

The forked child's tree replaces its task node, as for `spawn()`. Forked
children can use either `fork()` or `spawn()` to create more children. Wait
for the child with `os.waitpid()`, then end its task in the parent. Close the
child's reporter before `os._exit()`, which skips normal cleanup. Handle child
errors and exit status as required by your application.

Forks on disabled, ended, or closed nodes still run, but the child has no
`ZIG_PROGRESS` channel. A failed fork leaves the parent reporter usable.

Use `Node.fork()` to connect a forked process to a task. Direct `os.fork()`
calls and `multiprocessing` do not create this connection. `Node.fork()`
protects `zppy`'s own state. Application code and other libraries must also
meet the platform's [fork
requirements](https://docs.python.org/3/library/os.html#os.fork).

### API reference

`Progress(root_name="", estimated_total_items=0, *, refresh_rate=0.08,
initial_delay=0.2, fd=None, disable=False, name_size=120)` creates a reporter.
Use `progress.root` to access its root task.

| Option | Meaning |
| --- | --- |
| `root_name` | Root label, as `str` or `bytes`. |
| `estimated_total_items` | Estimated root item count. Zero means unknown. |
| `refresh_rate` | Seconds between reports. Use `None` for manual reports. |
| `initial_delay` | Seconds before the first automatic report. |
| `fd` | Explicit pipe descriptor. `None` reads `ZIG_PROGRESS`. |
| `disable` | Disable all reporting when true. |
| `name_size` | Name field size: `120` by default, or `40` for the old format. |

Timer values must be finite numbers no greater than `threading.TIMEOUT_MAX`.
`refresh_rate` must be positive, and `initial_delay` must be nonnegative.

`progress.enabled` shows whether the channel is open. `progress.flush()` tries
to send a snapshot immediately. It returns `True` if the current snapshot was
sent in full, or `False` if reporting is disabled or the write could not
finish. It does not wait for pipe space.

`progress.close()` stops the update thread, tries to send the final state, and
releases its descriptor. Use a context manager or call `close()` directly.
Closing the reporter does not set counts to their totals or remove the final
tree from the receiver. Ending the root also closes the reporter.

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
ended nodes also have no effect. Calls to `spawn()` and `fork()` still start a
process on ended nodes, as described in the child process sections. Invalid
arguments raise `TypeError` or `ValueError`, including when reporting is
disabled.

Counts must be nonnegative integers. Completed counts are limited to
`4294967295`. Totals are limited to `4294967294`, because Zig reserves the
next value. The library reduces larger values to these limits. Completed
counts can exceed totals.

Names are zero-padded to the field size. Text is encoded as UTF-8 and
shortened at a character boundary. Byte names are shortened without decoding.
Bytes after the first NUL are ignored.

The reporter tracks up to 253 active nodes, including the root. Additional
nodes have no effect. Ending nodes makes space for new nodes. Combined reports
also have a 253-node limit. Local nodes take priority, followed by child trees
in process creation order. Child roots replace local nodes without using more
slots. The reporter omits excess descendants and their branches. These
descendants can appear in later reports when space is available.

## Technical details

### Protocol compatibility

The default uses a 120-byte name field. Each node has two little-endian 32-bit
counters and 120 name bytes. A packet contains the node count, all node
records, then all parent indices. The root's parent is `255`.

The [published specification][protocol] uses 40-byte names. Select that format
explicitly with `Progress(name_size=40)`. The two formats are not wire
compatible. Both use `ZIG_PROGRESS`, and there is no version handshake. Select
the format that matches your receiver.

[protocol]: https://andrewkelley.me/post/zig-new-cli-progress-bar-explained.html#the-zig-progress-protocol-specification

Use one reporter per pipe. Each packet replaces the previous tree. A packet
does not add tasks to the previous tree. Multiple writers can replace each
other's progress or interleave packet bytes. Use `Node.spawn()` or
`Node.fork()` to combine progress from separate processes through private
pipes. Create a new reporter in each child, and use the same `name_size`
throughout the process tree.

### Platforms and pipe handling

The reporter accepts only writable POSIX pipes, such as those on Linux and
macOS. Other descriptors, including regular files and read-only pipes, disable
reporting without changing their data or blocking mode. Reporting is disabled
on other platforms.

The reporter duplicates the supplied descriptor and never closes the original.
The duplicate is not inherited across exec. The reporter sets the channel to
nonblocking mode. This flag also applies to the original descriptor. When you
create a reporter, it does not change `ZIG_PROGRESS` in the environment.
`Node.fork()` sets `ZIG_PROGRESS` in the forked child only.

A full pipe can delay or drop updates. The reporter resumes partial packets
before it sends a new snapshot. A permanent write error disables reporting.

Closing the reporter never waits for pipe space. The final report can be lost.
If the receiver stops reading, a large partial packet can remain incomplete.
If final delivery is required, retry `flush()` while the receiver is active
before you close the reporter.
