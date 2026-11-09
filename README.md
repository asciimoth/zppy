# zppy

Python reporter for the Zig progress protocol. It sends task trees to a parent
process through the `ZIG_PROGRESS` pipe. Use a receiver such as
[`zpg`](https://github.com/asciimoth/zpg) to display progress.

Requires Python 3.11 or later. The library has no runtime dependencies.
The pipe transport supports POSIX systems, including Linux and macOS.

## Usage

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
| `complete_one()` | Add one completed item. |
| `set_completed_items(count)` | Set the absolute completed count. |
| `set_estimated_total_items(count)` | Set the estimated total. |
| `increase_estimated_total_items(count)` | Add to the estimated total. |
| `set_name(name)` | Change the task label. |
| `end()` | Remove this branch and complete one item in its parent. |

Calls are thread-safe. Repeated `end()` calls have no effect. Operations on
ended nodes also have no effect. Invalid arguments raise `TypeError` or
`ValueError`, including when reporting is disabled.

Counts must be nonnegative integers. Completed counts are limited to
`4294967295`; totals are limited to `4294967294`, because Zig reserves the next
value. Larger values stop at these limits. Completed counts can exceed totals.

Names are zero-padded to the field size. Text is encoded as UTF-8 and shortened
at a character boundary. Byte names are shortened without decoding. Bytes after
the first NUL are ignored. The reporter tracks up to 253 active nodes, including
the root. Additional nodes are no-ops. Ending nodes makes space for new nodes.

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
atomic pipe write. Use nested `zpg` processes when you want to combine progress
from separate commands. The library does not collect progress from child
processes. Do not share a reporter across a process fork.

The reporter duplicates the supplied descriptor and never closes the original.
Its duplicate is not inherited across exec. It sets the channel to nonblocking
mode; this flag also applies to the original descriptor. It does not change
`ZIG_PROGRESS` in the environment.

A full pipe can delay or drop updates. Partial packets resume before a new
snapshot is sent. A permanent write error disables reporting. Closing never
waits for pipe space: a final report can be lost, or a large partial packet can
remain incomplete if the receiver stops reading. If final delivery is required,
retry `flush()` while the receiver is active before closing.
