"""Report task trees through a Zig progress pipe."""

from __future__ import annotations

import math
import os
import struct
import threading
from dataclasses import dataclass
from types import TracebackType

_MAX_NODES = 253
_MAX_COMPLETED = 0xFFFFFFFF
# Zig uses this value plus one to mark an internal IPC node.
_MAX_TOTAL = 0xFFFFFFFE


def _count(value: int) -> int:
    if not isinstance(value, int):
        raise TypeError("item counts must be integers")
    if value < 0:
        raise ValueError("item counts must be nonnegative")
    return value


def _name(value: str | bytes, size: int) -> bytes:
    if isinstance(value, str):
        # Do not split a UTF-8 character at the wire limit.
        return (
            value.split("\0", 1)[0]
            .encode("utf-8")[:size]
            .decode("utf-8", errors="ignore")
            .encode("utf-8")
        )
    if isinstance(value, bytes):
        return value.split(b"\0", 1)[0][:size]
    raise TypeError("name must be str or bytes")


def _environment_fd() -> int | None:
    value = os.environ.get("ZIG_PROGRESS", "")
    if not value or not value.isascii() or not value.isdecimal():
        return None
    # Avoid Python's limit on the number of digits in an integer string.
    value = value.lstrip("0") or "0"
    if len(value) > 5:
        return None
    fd = int(value)
    return fd if fd <= 65535 else None


@dataclass
class _State:
    parent: Node | None
    name: bytes
    total: int
    completed: int = 0


class Node:
    """A task in a Progress tree. Use start() to create a child task.

    Operations are thread-safe. end() removes the task and its descendants,
    then completes one item in its parent. Repeated end() calls and operations
    on ended or disabled nodes have no effect.
    """

    def __init__(self, progress: Progress | None = None) -> None:
        self._progress = progress

    def start(self, name: str | bytes, estimated_total_items: int = 0) -> Node:
        """Start a child task. Zero means the total is unknown."""
        progress = self._progress
        name_bytes = _name(name, progress.name_size if progress else 120)
        total = min(_count(estimated_total_items), _MAX_TOTAL)
        if progress is not None:
            with progress._lock:
                if self in progress._nodes and len(progress._nodes) < _MAX_NODES:
                    node = Node(progress)
                    progress._nodes[node] = _State(self, name_bytes, total)
                    return node
        return Node()

    def complete_one(self) -> None:
        """Complete one item, up to the protocol's counter limit."""
        progress = self._progress
        if progress is not None:
            with progress._lock:
                if state := progress._nodes.get(self):
                    state.completed = min(state.completed + 1, _MAX_COMPLETED)

    def set_completed_items(self, count: int) -> None:
        """Set an absolute completed count. Large counts saturate at u32 max."""
        count = min(_count(count), _MAX_COMPLETED)
        progress = self._progress
        if progress is not None:
            with progress._lock:
                if state := progress._nodes.get(self):
                    state.completed = count

    def set_estimated_total_items(self, count: int) -> None:
        """Set the total; zero means unknown. Large totals saturate at 2**32-2."""
        count = min(_count(count), _MAX_TOTAL)
        progress = self._progress
        if progress is not None:
            with progress._lock:
                if state := progress._nodes.get(self):
                    state.total = count

    def increase_estimated_total_items(self, count: int) -> None:
        """Add to the estimated total, up to 2**32-2."""
        count = _count(count)
        progress = self._progress
        if progress is not None:
            with progress._lock:
                if state := progress._nodes.get(self):
                    state.total = min(state.total + count, _MAX_TOTAL)

    def set_name(self, name: str | bytes) -> None:
        """Set the task label, limited to the reporter's name_size in bytes."""
        progress = self._progress
        name_bytes = _name(name, progress.name_size if progress else 120)
        if progress is not None:
            with progress._lock:
                if state := progress._nodes.get(self):
                    state.name = name_bytes

    def end(self) -> None:
        """End this task. Ending the root closes its reporter."""
        progress = self._progress
        if progress is None:
            return
        if self is progress.root:
            progress.close()
            return
        with progress._lock:
            state = progress._nodes.get(self)
            if state is None:
                return
            # Parents precede children in insertion order. Remove the whole
            # branch so no packet can contain an edge to a missing parent.
            removed = {self}
            for node, child in list(progress._nodes.items()):
                if node in removed or child.parent in removed:
                    removed.add(node)
                    del progress._nodes[node]
            if parent := progress._nodes.get(state.parent):
                parent.completed = min(parent.completed + 1, _MAX_COMPLETED)

    def __enter__(self) -> Node:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.end()


class Progress:
    """A reporter for the process-wide ZIG_PROGRESS channel.

    Construction starts a daemon thread unless refresh_rate is None. Times
    are in seconds. Missing, malformed, or unusable channels disable reporting.
    No terminal output is produced. Use one reporter per pipe and close it
    with a context manager or close(). The caller retains ownership of fd.
    """

    def __init__(
        self,
        root_name: str | bytes = "",
        estimated_total_items: int = 0,
        *,
        refresh_rate: float | None = 0.08,
        initial_delay: float = 0.2,
        fd: int | None = None,
        disable: bool = False,
        name_size: int = 120,
    ) -> None:
        if not isinstance(name_size, int) or name_size not in (40, 120):
            raise ValueError("name_size must be 40 (published) or 120 (Zig 0.17)")
        self._record = struct.Struct(f"<II{name_size}s")
        self._name_size = name_size
        name = _name(root_name, name_size)
        total = min(_count(estimated_total_items), _MAX_TOTAL)
        if refresh_rate is not None and (
            not math.isfinite(refresh_rate) or refresh_rate <= 0
        ):
            raise ValueError("refresh_rate must be positive and finite, or None")
        if not math.isfinite(initial_delay) or initial_delay < 0:
            raise ValueError("initial_delay must be nonnegative and finite")
        if fd is not None and (not isinstance(fd, int) or not 0 <= fd <= 65535):
            raise ValueError("fd must be an integer from 0 to 65535")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._pending = b""
        self._pending_snapshot = b""
        self._last_snapshot = b""
        self._closed = False
        self._nodes: dict[Node, _State] = {}
        self.root = Node(self)
        if not disable:
            source = _environment_fd() if fd is None else fd
            if source is not None:
                try:
                    self._fd = os.dup(source)
                    # Duplicates share this flag with the caller's descriptor.
                    # The protocol requires a nonblocking channel.
                    os.set_blocking(self._fd, False)
                except OSError:
                    self._disconnect()
        if self._fd is not None:
            self._nodes[self.root] = _State(None, name, total)
            if refresh_rate is not None:
                self._thread = threading.Thread(
                    target=self._run,
                    args=(initial_delay, refresh_rate),
                    name="zppy-progress",
                    daemon=True,
                )
                try:
                    self._thread.start()
                except RuntimeError:
                    self._thread = None
                    self._disconnect()

    @property
    def name_size(self) -> int:
        """Name field size: 120 by default, or 40 for the published protocol."""
        return self._name_size

    @property
    def enabled(self) -> bool:
        """Whether the reporter has an open progress channel."""
        with self._lock:
            return self._fd is not None

    def _snapshot(self) -> bytes:
        indices = {node: index for index, node in enumerate(self._nodes)}
        records = bytearray([len(indices)])
        parents = bytearray()
        for state in self._nodes.values():
            records.extend(self._record.pack(state.completed, state.total, state.name))
            parents.append(255 if state.parent is None else indices[state.parent])
        return bytes(records + parents)

    def _disconnect(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
        self._pending = b""
        self._pending_snapshot = b""
        self._last_snapshot = b""
        self._nodes.clear()
        self._stop.set()

    def _write(self, packet: bytes) -> bool:
        try:
            written = os.write(self._fd, packet)
        except (BlockingIOError, InterruptedError):
            # Keep an existing partial packet, but discard a new snapshot if
            # none of its bytes reached the pipe. The next one supersedes it.
            return False
        except OSError:
            self._disconnect()
            return False
        if written == 0:
            self._disconnect()
            return False
        if not self._pending:
            self._pending_snapshot = packet
        self._pending = packet[written:]
        if not self._pending:
            self._last_snapshot = self._pending_snapshot
            self._pending_snapshot = b""
        return not self._pending

    def flush(self) -> bool:
        """Try to send the current tree without waiting for pipe space.

        Return True if the current snapshot was sent in full. Partial writes
        resume before any newer snapshot. A full pipe delays or drops updates;
        a permanent I/O error disables reporting without raising an exception.
        """
        with self._lock:
            if self._fd is None or self._closed:
                return False
            snapshot = self._snapshot()
            if self._pending:
                pending_snapshot = self._pending_snapshot
                if not self._write(self._pending):
                    return False
                if pending_snapshot == snapshot:
                    return True
            return self._write(snapshot)

    def _run(self, initial_delay: float, refresh_rate: float) -> None:
        if self._stop.wait(initial_delay):
            return
        while not self._stop.is_set():
            self.flush()
            if self._stop.wait(refresh_rate):
                return

    def close(self) -> None:
        """Stop reports and try to send the final state. Safe to call again.

        This does not wait for a receiver to drain a full pipe. Final delivery
        is best effort; a pending large packet can remain incomplete if the
        pipe is still full. The original descriptor is never closed.
        """
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            if self._closed:
                return
            if self._fd is not None:
                snapshot = self._snapshot()
                pending_snapshot = self._pending_snapshot
                if not self._pending:
                    if snapshot != self._last_snapshot:
                        self._write(snapshot)
                elif self._write(self._pending) and pending_snapshot != snapshot:
                    self._write(snapshot)
            self._closed = True
            self._disconnect()

    def __enter__(self) -> Progress:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()
