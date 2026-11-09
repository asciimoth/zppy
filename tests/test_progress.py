import os
import select
import struct
import threading
import unittest
from unittest.mock import patch

from zppy import Node, Progress


def decode(data, name_size=120):
    """Decode complete packets, independently of the reporter implementation."""
    packets = []
    while data:
        count = data[0]
        record_size = 8 + name_size
        size = 1 + (record_size + 1) * count
        if len(data) < size:
            raise AssertionError("truncated progress packet")
        nodes = []
        for index in range(count):
            offset = 1 + record_size * index
            completed, total = struct.unpack_from("<II", data, offset)
            name = data[offset + 8 : offset + record_size]
            parent = data[1 + record_size * count + index]
            nodes.append((completed, total, name.rstrip(b"\0"), parent))
        packets.append(nodes)
        data = data[size:]
    return packets


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.addCleanup(os.close, self.read_fd)
        self.addCleanup(os.close, self.write_fd)

    def reporter(self, name="root", total=0, **kwargs):
        kwargs.setdefault("refresh_rate", None)
        progress = Progress(name, total, fd=self.write_fd, **kwargs)
        self.addCleanup(progress.close)
        return progress

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

    def test_specification_example_bytes(self):
        progress = self.reporter("preparing assets", name_size=40)
        child = progress.root.start("reticulating splines", 100)
        child.set_completed_items(50)
        self.assertTrue(progress.flush())
        expected = (
            b"\x02"
            + b"\0" * 8
            + b"preparing assets".ljust(40, b"\0")
            + b"\x32\0\0\0\x64\0\0\0"
            + b"reticulating splines".ljust(40, b"\0")
            + b"\xff\x00"
        )
        self.assertEqual(self.read(), expected)

    def test_default_format_has_120_byte_names(self):
        progress = self.reporter("x" * 120)
        self.assertEqual(progress.name_size, 120)
        progress.flush()
        packet = self.read()
        self.assertEqual(len(packet), 130)
        self.assertEqual(packet, b"\x01" + b"\0" * 8 + b"x" * 120 + b"\xff")

    def test_nested_tasks_and_index_remapping(self):
        progress = self.reporter("build", 2)
        first = progress.root.start("first")
        second = progress.root.start("second", 1)
        child = second.start("child", 5)
        first.end()
        first.end()
        replacement = progress.root.start("replacement")
        first.complete_one()
        first.set_name("stale")
        child.complete_one()
        progress.flush()
        self.assertEqual(
            decode(self.read()),
            [
                [
                    (1, 2, b"build", 255),
                    (0, 1, b"second", 0),
                    (1, 5, b"child", 1),
                    (0, 0, b"replacement", 0),
                ]
            ],
        )
        second.end()  # Also removes the active child.
        child.end()
        child.start("orphan").complete_one()
        replacement.end()
        progress.flush()
        self.assertEqual(decode(self.read()), [[(3, 2, b"build", 255)]])

    def test_context_managers_close_on_exception(self):
        progress = self.reporter()
        with self.assertRaisesRegex(RuntimeError, "work failed"):
            with progress:
                with progress.root.start("child"):
                    raise RuntimeError("work failed")
        self.assertFalse(progress.enabled)
        self.assertEqual(decode(self.read()), [[(1, 0, b"root", 255)]])
        # The caller still owns its descriptor.
        os.fstat(self.write_fd)
        progress.close()
        progress.root.end()
        progress.root.start("late").end()
        self.assertFalse(progress.flush())

    def test_counter_limits_and_unknown_total(self):
        progress = self.reporter(total=2**64)
        root = progress.root
        root.set_completed_items(2**64)
        root.complete_one()
        root.increase_estimated_total_items(2**64)
        progress.flush()
        self.assertEqual(decode(self.read())[0][0][:2], (0xFFFFFFFF, 0xFFFFFFFE))
        root.set_estimated_total_items(0)
        root.set_completed_items(0)
        root.complete_one()
        progress.flush()
        self.assertEqual(decode(self.read())[0][0][:2], (1, 0))
        root.increase_estimated_total_items(3)
        progress.flush()
        self.assertEqual(decode(self.read())[0][0][:2], (1, 3))

    def test_names_and_zero_padding(self):
        for size in (40, 120):
            progress = self.reporter("x" * (size - 1) + "é", name_size=size)
            progress.flush()
            self.assertEqual(decode(self.read(), size)[0][0][2], b"x" * (size - 1))
            for name, expected in [
                ("é" * (size // 2), "é".encode() * (size // 2)),
                (b"x" * (size + 1), b"x" * size),
                (b"\xff\0ignored", b"\xff"),
                ("short\0ignored", b"short"),
                ("", b""),
            ]:
                with self.subTest(size=size, name=name):
                    progress.root.set_name(name)
                    progress.flush()
                    packet = self.read()
                    self.assertEqual(packet[9 : 9 + size], expected.ljust(size, b"\0"))

    def test_invalid_arguments(self):
        root = self.reporter().root
        for method in [
            root.set_completed_items,
            root.set_estimated_total_items,
            root.increase_estimated_total_items,
        ]:
            with self.assertRaises(ValueError):
                method(-1)
            with self.assertRaises(TypeError):
                method(1.5)
        with self.assertRaises(TypeError):
            root.set_name(123)
        for kwargs in [
            {"refresh_rate": 0},
            {"refresh_rate": -1},
            {"refresh_rate": float("nan")},
            {"refresh_rate": float("inf")},
            {"initial_delay": -1},
            {"initial_delay": float("inf")},
            {"fd": -1},
            {"fd": 65536},
            {"estimated_total_items": -1},
            {"name_size": 80},
            {"name_size": 120.0},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                Progress(**kwargs)

    def test_absent_invalid_and_disabled_channels(self):
        values = [None, "", "-1", "+3", " 3", "3 ", "3.0", "٣", "65536", "9" * 5000]
        for value in values:
            with self.subTest(value=str(value)[:20]):
                env = {} if value is None else {"ZIG_PROGRESS": value}
                with patch.dict(os.environ, env, clear=True):
                    with Progress() as progress:
                        self.assertFalse(progress.enabled)
                        self.assertIsNone(progress._thread)
                        with progress.root.start("child") as child:
                            child.complete_one()
                        self.assertFalse(progress.flush())
        with Progress(fd=self.write_fd, disable=True) as progress:
            self.assertFalse(progress.enabled)
        Node().end()
        self.assertEqual(self.read(), b"")

    def test_environment_descriptor_and_leading_zeroes(self):
        value = "0" * 5000 + str(self.write_fd)
        with patch.dict(os.environ, {"ZIG_PROGRESS": value}):
            with Progress("env", refresh_rate=None) as progress:
                self.assertTrue(progress.enabled)
                self.assertFalse(os.get_inheritable(progress._fd))
        self.assertEqual(decode(self.read()), [[(0, 0, b"env", 255)]])

    def test_invalid_descriptor_is_disabled(self):
        closed = os.dup(self.write_fd)
        os.close(closed)
        with Progress(fd=closed) as progress:
            self.assertFalse(progress.enabled)

    def test_full_pipe_drops_unsent_snapshot(self):
        progress = self.reporter()
        filler = b"x" * 4096
        while True:
            try:
                os.write(self.write_fd, filler)
            except BlockingIOError:
                break
        self.assertFalse(progress.flush())
        self.assertTrue(progress.enabled)
        self.assertEqual(set(self.read()), {ord("x")})
        progress.root.set_completed_items(42)
        self.assertTrue(progress.flush())
        self.assertEqual(decode(self.read()), [[(42, 0, b"root", 255)]])

    def test_partial_write_resumes_before_new_snapshot(self):
        progress = self.reporter()
        write = os.write
        with patch(
            "zppy.progress.os.write", side_effect=lambda fd, b: write(fd, b[:7])
        ):
            self.assertFalse(progress.flush())
        progress.root.set_completed_items(42)
        with patch("zppy.progress.os.write", side_effect=BlockingIOError):
            self.assertFalse(progress.flush())
        self.assertTrue(progress.flush())
        self.assertEqual(
            decode(self.read()), [[(0, 0, b"root", 255)], [(42, 0, b"root", 255)]]
        )

    def test_interrupted_write_can_retry(self):
        progress = self.reporter()
        with patch("zppy.progress.os.write", side_effect=InterruptedError):
            self.assertFalse(progress.flush())
        self.assertTrue(progress.flush())
        self.assertEqual(len(decode(self.read())), 1)

    def test_permanent_write_error_disables_reporter(self):
        for error in [BrokenPipeError(), OSError("bad descriptor")]:
            progress = self.reporter()
            with patch("zppy.progress.os.write", side_effect=error):
                self.assertFalse(progress.flush())
                self.assertFalse(progress.enabled)
                progress.close()

    def test_closed_reader_is_harmless(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        try:
            with Progress(fd=write_fd, refresh_rate=None) as progress:
                self.assertFalse(progress.flush())
                self.assertFalse(progress.enabled)
        finally:
            os.close(write_fd)

    def test_node_limit_reuse_and_large_packets(self):
        progress = self.reporter()
        nodes = [progress.root.start(str(index)) for index in range(252)]
        ignored = progress.root.start("overflow")
        ignored.start("also ignored").complete_one()
        ignored.end()
        data = bytearray()
        for _ in range(20):
            complete = progress.flush()
            data.extend(self.read())
            if complete:
                break
        self.assertTrue(complete, "large snapshot did not finish")
        packets = decode(bytes(data))
        self.assertEqual(len(packets), 1)
        packet = packets[0]
        self.assertEqual(len(packet), 253)
        self.assertEqual(packet[0], (0, 0, b"root", 255))
        self.assertTrue(all(node[3] == 0 for node in packet[1:]))
        nodes[0].end()
        replacement = progress.root.start("replacement")
        self.assertIsNotNone(replacement._progress)
        self.assertIsNone(ignored._progress)

    def test_threaded_updates_and_prompt_shutdown(self):
        progress = self.reporter(initial_delay=0, refresh_rate=0.01)
        self.assertTrue(select.select([self.read_fd], [], [], 2)[0])
        self.assertTrue(decode(self.read()))
        progress.close()
        self.assertFalse(progress._thread.is_alive())
        delayed = self.reporter(initial_delay=3600, refresh_rate=3600)
        delayed.root.end()
        self.assertFalse(delayed._thread.is_alive())

    def test_close_does_not_resend_a_delivered_large_snapshot(self):
        progress = self.reporter()
        for _ in range(252):
            progress.root.start("task")
        data = bytearray()
        for _ in range(100):
            complete = progress.flush()
            data.extend(self.read())
            if complete:
                break
        self.assertTrue(complete)
        self.assertEqual(len(decode(bytes(data))[0]), 253)
        progress.close()
        self.assertEqual(self.read(), b"")

    def test_concurrent_updates_preserve_counts_and_tree(self):
        progress = self.reporter()

        def work():
            for _ in range(100):
                with progress.root.start("work") as node:
                    for _ in range(10):
                        node.complete_one()

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        progress.flush()
        self.assertEqual(decode(self.read()), [[(800, 0, b"root", 255)]])


if __name__ == "__main__":
    unittest.main()
