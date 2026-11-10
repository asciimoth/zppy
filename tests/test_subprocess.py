"""Child process transport, tree merging, and resource ownership tests."""

import os
import random
import select
import struct
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_progress import decode
from zppy import Node, Progress
from zppy.progress import _Child


def packet(nodes, name_size=120):
    records = b"".join(
        struct.pack(f"<II{name_size}s", completed, total, name)
        for completed, total, name, _ in nodes
    )
    return bytes([len(nodes)]) + records + bytes(node[3] for node in nodes)


@unittest.skipUnless(os.name == "posix", "progress pipes require POSIX")
class SubprocessTests(unittest.TestCase):
    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.addCleanup(os.close, self.read_fd)
        self.addCleanup(os.close, self.write_fd)
        self.env = os.environ.copy()
        self.env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")

    def reporter(self, **kwargs):
        kwargs.setdefault("refresh_rate", None)
        progress = Progress("parent", fd=self.write_fd, **kwargs)
        self.addCleanup(progress.close)
        return progress

    def worker(self, node, source, **kwargs):
        kwargs.setdefault("env", self.env)
        process = node.spawn(
            [sys.executable, "-u", "-c", textwrap.dedent(source)], **kwargs
        )

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(cleanup)
        return process

    def read(self):
        result = bytearray()
        while True:
            try:
                chunk = os.read(self.read_fd, 65536)
            except BlockingIOError:
                break
            if not chunk:
                break
            result.extend(chunk)
        return bytes(result)

    def snapshot(self, progress):
        data = bytearray()
        for _ in range(100):
            complete = progress.flush()
            data.extend(self.read())
            if complete:
                return decode(bytes(data), progress.name_size)[-1]
        self.fail("snapshot did not finish")

    def attach(self, progress, node):
        # Inject raw wire data independently of the child reporter.
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        self.addCleanup(os.close, write_fd)
        child = _Child(read_fd)
        progress._children[node] = child
        return child, write_fd

    def test_sibling_processes_and_explicit_completion(self):
        progress = self.reporter(estimated_total_items=2)
        nodes = [progress.root.start(name) for name in ("first", "second")]
        for index, node in enumerate(nodes):
            process = self.worker(
                node,
                f"""
                import sys
                from zppy import Progress
                with Progress('worker-{index}', 4, refresh_rate=None) as p:
                    p.root.set_completed_items({index + 1})
                    p.root.start('leaf-{index}', 10).set_completed_items(3)
                print('stdout')
                print('stderr', file=sys.stderr)
                """,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(process.communicate(timeout=5), (b"stdout\n", b"stderr\n"))
            self.assertEqual(process.returncode, 0)
        self.assertEqual(
            self.snapshot(progress),
            [
                (0, 2, b"parent", 255),
                (1, 4, b"worker-0", 0),
                (2, 4, b"worker-1", 0),
                (3, 10, b"leaf-0", 1),
                (3, 10, b"leaf-1", 2),
            ],
        )
        # EOF retains the last tree until the caller ends its task.
        self.assertTrue(all(child.fd is None for child in progress._children.values()))
        nodes[0].end()
        nodes[0].end()
        self.assertEqual(
            self.snapshot(progress),
            [(1, 2, b"parent", 255), (2, 4, b"worker-1", 0), (3, 10, b"leaf-1", 1)],
        )
        nodes[1].end()
        self.assertEqual(self.snapshot(progress), [(2, 2, b"parent", 255)])

    def test_grandchild_tree_in_both_formats(self):
        for size in (40, 120):
            with self.subTest(size=size):
                progress = self.reporter(name_size=size)
                grandchild = (
                    "from zppy import Progress\n"
                    f"with Progress('grandchild', name_size={size}, "
                    "refresh_rate=None) as p:\n"
                    "    p.root.start('work', 9).set_completed_items(7)\n"
                )
                process = self.worker(
                    progress.root.start("child task"),
                    f"""
                    import sys
                    from zppy import Progress
                    with Progress('child', name_size={size}, refresh_rate=None) as p:
                        node = p.root.start('grandchild task')
                        proc = node.spawn([sys.executable, '-c', {grandchild!r}])
                        assert proc.wait(timeout=5) == 0
                    """,
                )
                self.assertEqual(process.wait(timeout=5), 0)
                self.assertEqual(
                    self.snapshot(progress),
                    [
                        (0, 0, b"parent", 255),
                        (0, 0, b"child", 0),
                        (0, 0, b"grandchild", 1),
                        (7, 9, b"work", 2),
                    ],
                )
                progress.close()
                self.read()

    def test_automatic_collection_while_child_is_running(self):
        progress = self.reporter(initial_delay=0, refresh_rate=0.01)
        process = self.worker(
            progress.root.start("worker"),
            """
            from zppy import Progress
            with Progress('running', 10, refresh_rate=None) as p:
                p.root.set_completed_items(6)
                assert p.flush()
                input()
            """,
            stdin=subprocess.PIPE,
        )
        deadline = time.monotonic() + 5
        found = False
        while time.monotonic() < deadline:
            if select.select([self.read_fd], [], [], 0.1)[0]:
                if any((6, 10, b"running", 0) in tree for tree in decode(self.read())):
                    found = True
                    break
        self.assertTrue(found, "background thread did not forward child progress")
        self.assertIsNone(process.poll())
        process.communicate(b"\n", timeout=5)

    def test_custom_environment_and_pass_fds(self):
        progress = self.reporter()
        extra_read, extra_write = os.pipe()
        self.addCleanup(os.close, extra_read)
        self.addCleanup(os.close, extra_write)
        for env in (
            {"CUSTOM": "value", "ZIG_PROGRESS": str(self.write_fd)},
            {b"CUSTOM": b"value", b"ZIG_PROGRESS": str(self.write_fd).encode()},
        ):
            original = env.copy()
            with progress.root.start("worker") as node:
                process = self.worker(
                    node,
                    f"""
                    import os
                    fd = int(os.environ['ZIG_PROGRESS'])
                    assert not os.get_blocking(fd)
                    assert fd != {self.write_fd}
                    for closed in ({self.write_fd}, {progress._fd}):
                        try:
                            os.fstat(closed)
                        except OSError:
                            pass
                        else:
                            raise AssertionError('upstream descriptor inherited')
                    os.write({extra_write}, b'ok')
                    print(os.environ['CUSTOM'])
                    print(os.getcwd())
                    """,
                    env=env,
                    pass_fds=(extra_write,),
                    cwd="/",
                    stdout=subprocess.PIPE,
                    text=True,
                )
                self.assertEqual(process.communicate(timeout=5)[0], "value\n/\n")
                self.assertEqual(process.returncode, 0)
            self.assertEqual(env, original)
            self.assertEqual(os.read(extra_read, 2), b"ok")

    def test_disabled_ended_and_closed_nodes_still_spawn(self):
        progress = self.reporter()
        ended = progress.root.start("ended")
        ended.end()
        disabled = self.reporter(disable=True)
        closed = self.reporter()
        closed.close()
        self.read()
        for node in (Node(), ended, disabled.root, closed.root):
            with self.subTest(node=node):
                process = self.worker(
                    node,
                    "import os; assert 'ZIG_PROGRESS' not in os.environ",
                    env={"ZIG_PROGRESS": str(self.write_fd)},
                )
                self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(self.read(), b"")

    def test_spawn_failure_closes_both_pipe_ends_and_allows_retry(self):
        progress = self.reporter()
        node = progress.root.start("worker")
        pipe = os.pipe
        created = []

        def capture_pipe():
            pair = pipe()
            created.extend(pair)
            return pair

        with patch("zppy.progress.os.pipe", side_effect=capture_pipe):
            with self.assertRaises(FileNotFoundError):
                node.spawn(["/does-not-exist/zppy-test"])
        self.assertFalse(progress._children)
        for fd in created:
            with self.assertRaises(OSError):
                os.fstat(fd)
        process = self.worker(node, "pass")
        self.assertEqual(process.wait(timeout=5), 0)
        with self.assertRaisesRegex(ValueError, "already attached"):
            node.spawn([sys.executable, "-c", "pass"])
        with self.assertRaisesRegex(ValueError, "close_fds"):
            Node().spawn([sys.executable, "-c", "pass"], close_fds=False)

    def test_fragmented_coalesced_packets_forward_parents_and_eof(self):
        progress = self.reporter()
        node = progress.root.start("fallback")
        child, write_fd = self.attach(progress, node)
        first = packet([(1, 5, b"", 255)])
        # A forward parent reference is valid on the wire.
        second = packet([(2, 5, b"new", 255), (4, 8, b"leaf", 2), (0, 1, b"branch", 0)])
        os.write(write_fd, first[:7])
        self.assertEqual(self.snapshot(progress)[1], (0, 0, b"fallback", 0))
        os.write(write_fd, first[7:] + second[:19])
        self.assertEqual(self.snapshot(progress)[1], (1, 5, b"fallback", 0))
        os.write(write_fd, second[19:] + first + second)
        expected = [
            (0, 0, b"parent", 255),
            (2, 5, b"new", 0),
            (0, 1, b"branch", 1),
            (4, 8, b"leaf", 2),
        ]
        self.assertEqual(self.snapshot(progress), expected)
        os.write(write_fd, first[:11])
        with patch("zppy.progress.os.read", return_value=b""):
            child.read(progress._record)
        self.assertIsNone(child.fd)
        self.assertFalse(child.pending)
        self.assertEqual(self.snapshot(progress), expected)

    def test_invalid_child_data_keeps_last_tree_and_other_nodes(self):
        invalid = [
            b"\0",
            b"\xfe",
            packet([(0, 0, b"bad root", 0)]),
            packet([(0, 0, b"root", 255), (0, 0, b"missing", 2)]),
            packet([(0, 0, b"root", 255), (0, 0, b"second root", 255)]),
            packet([(0, 0, b"root", 255), (0, 0, b"self", 1)]),
            packet([(0, 0, b"root", 255), (0, 0, b"a", 2), (0, 0, b"b", 1)]),
            packet([(0, 0xFFFFFFFF, b"reserved", 255)]),
        ]
        progress = self.reporter()
        for data in invalid:
            with self.subTest(data=data[:10]), progress.root.start("worker") as node:
                child, write_fd = self.attach(progress, node)
                os.write(write_fd, packet([(1, 2, b"valid", 255)]) + data)
                self.assertEqual(self.snapshot(progress)[1], (1, 2, b"valid", 0))
                self.assertIsNone(child.fd)
                self.assertTrue(progress.enabled)

    def test_combined_capacity_preserves_local_nodes_and_restores_remote_tree(self):
        progress = self.reporter()
        worker = progress.root.start("worker")
        _, write_fd = self.attach(progress, worker)
        local = [progress.root.start("local") for _ in range(250)]
        os.write(
            write_fd,
            packet([(1, 2, b"remote", 255), (0, 1, b"branch", 0), (0, 0, b"leaf", 1)]),
        )
        tree = self.snapshot(progress)
        self.assertEqual(len(tree), 253)
        self.assertEqual(tree[1], (1, 2, b"remote", 0))
        self.assertEqual(tree[-1], (0, 1, b"branch", 1))
        for node in local:
            node.end()
        self.assertEqual(
            self.snapshot(progress),
            [
                (250, 0, b"parent", 255),
                (1, 2, b"remote", 0),
                (0, 1, b"branch", 1),
                (0, 0, b"leaf", 2),
            ],
        )

    def test_end_ancestor_and_disconnect_close_child_channels(self):
        for action in ("end", "close", "disconnect"):
            with self.subTest(action=action):
                progress = self.reporter()
                ancestor = progress.root.start("ancestor")
                child, _ = self.attach(progress, ancestor.start("worker"))
                read_fd = child.fd
                if action == "end":
                    ancestor.end()
                elif action == "close":
                    progress.close()
                else:
                    with patch("zppy.progress.os.write", side_effect=BrokenPipeError):
                        self.assertFalse(progress.flush())
                self.assertFalse(progress._children)
                with self.assertRaises(OSError):
                    os.fstat(read_fd)

    def test_no_reports_and_nonzero_exit_do_not_complete_task(self):
        progress = self.reporter()
        with progress.root.start("silent") as node:
            process = self.worker(node, "raise SystemExit(7)")
            self.assertEqual(process.wait(timeout=5), 7)
            self.assertEqual(
                self.snapshot(progress), [(0, 0, b"parent", 255), (0, 0, b"silent", 0)]
            )
        self.assertEqual(self.snapshot(progress), [(1, 0, b"parent", 255)])

    def test_collects_child_updates_while_upstream_is_full(self):
        progress = self.reporter()
        _, write_fd = self.attach(progress, progress.root.start("worker"))
        while True:
            try:
                os.write(self.write_fd, b"x" * 4096)
            except BlockingIOError:
                break
        for count in range(10):
            os.write(write_fd, packet([(count, 10, b"child", 255)]))
            self.assertFalse(progress.flush())
        self.assertEqual(set(self.read()), {ord("x")})
        self.assertEqual(
            self.snapshot(progress), [(0, 0, b"parent", 255), (9, 10, b"child", 0)]
        )

    def test_child_changes_do_not_corrupt_partial_upstream_packet(self):
        progress = self.reporter()
        _, write_fd = self.attach(progress, progress.root.start("worker"))
        os.write(write_fd, packet([(1, 10, b"old", 255)]))
        write = os.write
        with patch(
            "zppy.progress.os.write", side_effect=lambda fd, b: write(fd, b[:9])
        ):
            self.assertFalse(progress.flush())
        os.write(write_fd, packet([(2, 10, b"new", 255), (0, 0, b"leaf", 0)]))
        self.assertTrue(progress.flush())
        self.assertEqual(
            decode(self.read()),
            [
                [(0, 0, b"parent", 255), (1, 10, b"old", 0)],
                [(0, 0, b"parent", 255), (2, 10, b"new", 0), (0, 0, b"leaf", 1)],
            ],
        )

    def test_every_packet_split_in_both_formats(self):
        for size in (40, 120):
            progress = self.reporter(name_size=size)
            nodes = [(2, 3, b"root", 255), (1, 5, b"leaf", 0)]
            wire = packet(nodes, size)
            for split in range(1, len(wire)):
                with self.subTest(size=size, split=split):
                    read_fd, write_fd = os.pipe()
                    os.set_blocking(read_fd, False)
                    child = _Child(read_fd)
                    try:
                        os.write(write_fd, wire[:split])
                        child.read(progress._record)
                        self.assertEqual(child.snapshot, [])
                        os.write(write_fd, wire[split:])
                        child.read(progress._record)
                        self.assertEqual(child.snapshot, nodes)
                    finally:
                        child.close()
                        os.close(write_fd)

    def test_random_trees_with_shuffled_indices_and_fragmented_updates(self):
        rng = random.Random(24071)
        for size in (40, 120):
            progress = self.reporter(name_size=size)
            child, write_fd = self.attach(progress, progress.root.start("worker"))
            for iteration in range(30):
                count = rng.randint(1, 100)
                parents = [255] + [rng.randrange(i) for i in range(1, count)]
                order = [0] + rng.sample(range(1, count), count - 1)
                remap = {old: new for new, old in enumerate(order)}
                names = [f"node-{iteration}-{i}".encode() for i in range(count)]
                nodes = [
                    (i, 100, names[i], 255 if i == 0 else remap[parents[i]])
                    for i in order
                ]
                wire = packet(nodes, size)
                while wire:
                    length = rng.randint(1, min(2048, len(wire)))
                    os.write(write_fd, wire[:length])
                    wire = wire[length:]
                    child.read(progress._record)
                tree = self.snapshot(progress)
                actual = {
                    name: (completed, total, tree[parent][2])
                    for completed, total, name, parent in tree[1:]
                }
                expected = {
                    names[i]: (i, 100, b"parent" if i == 0 else names[parents[i]])
                    for i in range(count)
                }
                self.assertEqual(actual, expected, (size, iteration))
            progress.close()
            self.read()

    def test_full_remote_tree_is_bounded_and_has_no_missing_parents(self):
        progress = self.reporter()
        child, write_fd = self.attach(progress, progress.root.start("worker"))
        # A maximum-depth tree also checks that parsing does not recurse.
        nodes = [(0, 0, b"remote", 255)] + [
            (i, 0, str(i).encode(), i - 1) for i in range(1, 253)
        ]
        wire = packet(nodes)
        for offset in range(0, len(wire), 2048):
            os.write(write_fd, wire[offset : offset + 2048])
            child.read(progress._record)
        tree = self.snapshot(progress)
        self.assertEqual(len(tree), 253)
        self.assertEqual(tree[1], (0, 0, b"remote", 0))
        self.assertTrue(all(node[3] == i - 1 for i, node in enumerate(tree[1:], 1)))
        self.assertEqual(tree[-1][2], b"251")

    def test_child_read_errors_are_isolated_and_interruptions_can_retry(self):
        progress = self.reporter()
        node = progress.root.start("worker")
        child, write_fd = self.attach(progress, node)
        os.write(write_fd, packet([(3, 5, b"reported", 255)]))
        for error in (InterruptedError(), BlockingIOError()):
            with patch("zppy.progress.os.read", side_effect=error):
                self.assertTrue(progress.flush())
            self.assertEqual(decode(self.read())[-1][1][2], b"worker")
            self.assertIsNotNone(child.fd)
        self.assertEqual(self.snapshot(progress)[1], (3, 5, b"reported", 0))
        with patch("zppy.progress.os.read", side_effect=OSError("read failed")):
            self.assertTrue(progress.flush())
        self.assertEqual(decode(self.read())[-1][1], (3, 5, b"reported", 0))
        self.assertIsNone(child.fd)
        self.assertTrue(progress.enabled)

    def test_continuous_child_writer_cannot_monopolize_an_update(self):
        progress = self.reporter()
        child, _ = self.attach(progress, progress.root.start("worker"))
        wire = packet([(1, 0, b"busy", 255)])
        received = 0

        def read(fd, size):
            nonlocal received
            # Honour read's byte limit, with an endless stream of valid packets.
            result = bytes(wire[(received + i) % len(wire)] for i in range(size))
            received += len(result)
            return result

        with patch("zppy.progress.os.read", side_effect=read):
            child.read(progress._record)
        self.assertEqual(received, 65536)
        self.assertEqual(child.snapshot, [(1, 0, b"busy", 255)])
        self.assertLess(len(child.pending), len(wire))

    def test_truncated_packet_at_real_eof_keeps_previous_report(self):
        progress = self.reporter()
        valid = packet([(2, 5, b"valid", 255)])
        truncated = packet([(4, 5, b"incomplete", 255)])[:17]
        process = self.worker(
            progress.root.start("worker"),
            "import os\n"
            f"os.write(int(os.environ['ZIG_PROGRESS']), {valid + truncated!r})",
        )
        self.assertEqual(process.wait(timeout=5), 0)
        self.assertEqual(self.snapshot(progress)[1], (2, 5, b"valid", 0))

    def test_invalid_child_does_not_block_valid_sibling(self):
        progress = self.reporter()
        bad, bad_fd = self.attach(progress, progress.root.start("bad"))
        _, good_fd = self.attach(progress, progress.root.start("good"))
        os.write(bad_fd, b"\xff")
        os.write(good_fd, packet([(5, 6, b"healthy", 255)]))
        self.assertEqual(
            self.snapshot(progress),
            [(0, 0, b"parent", 255), (0, 0, b"bad", 0), (5, 6, b"healthy", 0)],
        )
        self.assertIsNone(bad.fd)

    def test_ending_task_does_not_terminate_running_process(self):
        progress = self.reporter()
        node = progress.root.start("worker")
        process = self.worker(
            node,
            "input(); print('still running')",
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        node.end()
        self.assertIsNone(process.poll())
        self.assertEqual(process.communicate(b"go\n", timeout=5)[0], b"still running\n")
        self.assertEqual(self.snapshot(progress), [(1, 0, b"parent", 255)])

    def test_concurrent_spawns_keep_distinct_channels(self):
        progress = self.reporter()
        failures = []

        def work(index):
            try:
                node = progress.root.start(f"slot-{index}")
                process = node.spawn(
                    [
                        sys.executable,
                        "-c",
                        "from zppy import Progress\n"
                        f"with Progress('worker-{index}', refresh_rate=None): pass",
                    ],
                    env=self.env,
                )
                try:
                    self.assertEqual(process.wait(timeout=5), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
            except BaseException as error:
                failures.append(error)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        tree = self.snapshot(progress)
        self.assertEqual(
            {node[2] for node in tree[1:]}, {f"worker-{i}".encode() for i in range(8)}
        )
        self.assertTrue(all(node[3] == 0 for node in tree[1:]))


if __name__ == "__main__":
    unittest.main()
