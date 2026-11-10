"""Run fork scenarios in isolated process groups with a timeout."""

import os
import signal
import subprocess
import sys
import textwrap
import threading
import unittest
from pathlib import Path

from tests.test_progress import decode

PRELUDE = """
import os
import sys
import traceback
from zppy import Node, Progress

def fork_worker(node, work):
    pid = node.fork()
    if pid == 0:
        try:
            work()
        except BaseException:
            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    return pid

def wait(pid):
    actual, status = os.waitpid(pid, 0)
    assert actual == pid
    assert os.waitstatus_to_exitcode(status) == 0, status
"""


@unittest.skipUnless(hasattr(os, "fork"), "requires os.fork()")
class ForkTests(unittest.TestCase):
    def run_program(self, source, name_size=120):
        read_fd, write_fd = os.pipe()
        env = os.environ.copy()
        env["ZIG_PROGRESS"] = str(write_fd)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        data = bytearray()

        def read():
            while chunk := os.read(read_fd, 65536):
                data.extend(chunk)

        reader = threading.Thread(target=read, daemon=True)
        try:
            process = subprocess.Popen(
                [sys.executable, "-c", PRELUDE + textwrap.dedent(source)],
                env=env,
                pass_fds=(write_fd,),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        finally:
            os.close(write_fd)
        reader.start()
        try:
            try:
                stdout, stderr = process.communicate(timeout=10)
            finally:
                # Kill any descendants too, so a deadlock cannot leave the
                # reader waiting forever for an inherited write descriptor.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate(timeout=5)
                reader.join(timeout=5)
            self.assertFalse(reader.is_alive(), "progress pipe was not closed")
            self.assertEqual(process.returncode, 0, stderr.decode())
            self.assertEqual(stdout, b"")
            return decode(bytes(data), name_size)
        finally:
            os.close(read_fd)

    def test_siblings_replace_nodes_and_complete_once(self):
        trees = self.run_program("""
            original_env = os.environ['ZIG_PROGRESS']
            with Progress('parent', 2, refresh_rate=None) as parent:
                first = parent.root.start('first')
                second = parent.root.start('fallback')
                old_fd = parent._fd

                def work():
                    assert not parent.enabled
                    assert parent._thread is None
                    assert not parent.flush()
                    parent.root.complete_one()
                    parent.close()
                    try:
                        os.fstat(old_fd)
                    except OSError:
                        pass
                    else:
                        raise AssertionError('parent reporter fd still open')
                    fd = int(os.environ['ZIG_PROGRESS'])
                    assert str(fd) != original_env
                    assert not os.get_blocking(fd)
                    assert not os.get_inheritable(fd)
                    with Progress('worker', 5, refresh_rate=None) as child:
                        child.root.set_completed_items(2)
                        child.root.start('leaf', 9).set_completed_items(4)

                def unnamed():
                    with Progress('', 3, refresh_rate=None) as child:
                        child.root.complete_one()

                a = fork_worker(first, work)
                b = fork_worker(second, unnamed)
                wait(a)
                wait(b)
                assert os.environ['ZIG_PROGRESS'] == original_env
                assert parent.enabled
                assert parent.flush()
                assert all(c.fd is None for c in parent._children.values())
                first.end()
                first.end()
                assert parent.flush()
                second.end()
        """)
        self.assertIn(
            [
                (0, 2, b"parent", 255),
                (2, 5, b"worker", 0),
                (1, 3, b"fallback", 0),
                (4, 9, b"leaf", 1),
            ],
            trees,
        )
        self.assertIn([(1, 2, b"parent", 255), (1, 3, b"fallback", 0)], trees)
        self.assertEqual(trees[-1], [(2, 2, b"parent", 255)])

    def test_nested_forks_in_both_formats(self):
        for size in (40, 120):
            with self.subTest(size=size):
                trees = self.run_program(
                    f"""
                    def grandchild():
                        with Progress('grandchild', name_size={size},
                                      refresh_rate=None) as p:
                            p.root.start('work', 10).set_completed_items(7)

                    def child():
                        with Progress('child', name_size={size},
                                      refresh_rate=None) as p:
                            task = p.root.start('grandchild task')
                            old_channel = int(os.environ['ZIG_PROGRESS'])
                            def run():
                                try:
                                    os.fstat(old_channel)
                                except OSError:
                                    pass
                                else:
                                    raise AssertionError('ancestor channel open')
                                grandchild()
                            wait(fork_worker(task, run))

                    with Progress('parent', name_size={size},
                                  refresh_rate=None) as p:
                        wait(fork_worker(p.root.start('child task'), child))
                    """,
                    name_size=size,
                )
                self.assertEqual(
                    trees[-1],
                    [
                        (0, 0, b"parent", 255),
                        (0, 0, b"child", 0),
                        (0, 0, b"grandchild", 1),
                        (7, 10, b"work", 2),
                    ],
                )

    def test_fork_from_thread_with_live_reporters_and_locked_event(self):
        trees = self.run_program("""
            import threading
            import time

            with Progress('parent', initial_delay=0, refresh_rate=0.01) as parent:
                read_fd, write_fd = os.pipe()
                with Progress('unrelated', fd=write_fd, initial_delay=3600) as other:
                    task = parent.root.start('task')
                    # Hold an Event's internal lock across the fork. Child
                    # cleanup must replace the Event, without calling set().
                    ready = threading.Event()
                    release = threading.Event()
                    def hold():
                        with other._stop._cond:
                            ready.set()
                            release.wait()
                    holder = threading.Thread(target=hold)
                    holder.start()
                    ready.wait()

                    def child():
                        other.close()
                        parent.close()
                        assert not other.enabled
                        assert not parent.enabled
                        with Progress('automatic', 10, initial_delay=0,
                                      refresh_rate=0.01) as p:
                            p.root.set_completed_items(6)
                            time.sleep(0.1)
                    failures = []
                    def fork_in_thread():
                        try:
                            wait(fork_worker(task, child))
                        except BaseException as error:
                            failures.append(error)
                    thread = threading.Thread(target=fork_in_thread)
                    thread.start()
                    thread.join()
                    release.set()
                    holder.join()
                    assert not failures, failures
                    assert parent.flush()
                os.close(read_fd)
                os.close(write_fd)
        """)
        self.assertTrue(any((6, 10, b"automatic", 0) in tree for tree in trees), trees)

    def test_inherited_partial_packet_and_sibling_reader_are_discarded(self):
        trees = self.run_program("""
            from unittest.mock import patch

            with Progress('parent', refresh_rate=None) as p:
                first = p.root.start('first')
                second = p.root.start('second')
                def child():
                    with Progress('child', refresh_rate=None) as c:
                        c.root.complete_one()
                wait(fork_worker(first, child))
                write = os.write
                with patch('zppy.progress.os.write',
                           side_effect=lambda fd, b: write(fd, b[:7])):
                    assert not p.flush()
                assert p._pending
                def second_child():
                    assert not p._pending
                    assert not p._children
                    try:
                        os.fstat(sibling_fd)
                    except OSError:
                        pass
                    else:
                        raise AssertionError('sibling reader inherited')
                    with Progress('second child', refresh_rate=None) as c:
                        c.root.set_completed_items(3)
                # The first flush consumed EOF and closed the reader. Create
                # another live channel to check cleanup across this fork.
                read_fd, write_fd = os.pipe()
                from zppy.progress import _Child
                p._children[first] = _Child(read_fd)
                sibling_fd = read_fd
                wait(fork_worker(second, second_child))
                os.close(write_fd)
                assert p.flush()
        """)
        self.assertEqual(trees[0][1], (1, 0, b"child", 0))
        self.assertIn((3, 0, b"second child", 0), trees[-1])

    def test_disabled_ended_closed_and_capacity_nodes(self):
        trees = self.run_program("""
            with Progress('parent', refresh_rate=None) as p:
                original_env = os.environ['ZIG_PROGRESS']
                ended = p.root.start('ended')
                ended.end()
                closed = Progress(disable=True)
                closed.close()
                disabled = Progress(disable=True)
                for i in range(252):
                    p.root.start('local')
                overflow = p.root.start('overflow')
                def child():
                    assert 'ZIG_PROGRESS' not in os.environ
                    assert not p.enabled
                    with Progress('ignored') as c:
                        assert not c.enabled
                for node in (Node(), ended, closed.root, disabled.root, overflow):
                    wait(fork_worker(node, child))
                    assert os.environ['ZIG_PROGRESS'] == original_env
                # Keep the final packet small.
                for node in list(p._nodes)[1:]:
                    node.end()
        """)
        self.assertEqual(trees, [[(253, 0, b"parent", 255)]])

    def test_fork_failure_cleans_up_and_can_retry(self):
        trees = self.run_program("""
            from unittest.mock import patch

            with Progress('parent', refresh_rate=None) as p:
                task = p.root.start('task')
                original_env = os.environ['ZIG_PROGRESS']
                pipe = os.pipe
                created = []
                def capture():
                    pair = pipe()
                    created.extend(pair)
                    return pair
                for failed_call in ('fork', 'set_blocking'):
                    with patch('zppy.progress.os.pipe', side_effect=capture):
                        with patch('zppy.progress.os.' + failed_call,
                                   side_effect=OSError('failure')):
                            try:
                                task.fork()
                            except OSError:
                                pass
                            else:
                                raise AssertionError('fork did not fail')
                    assert not p._children
                    assert p.enabled
                    assert os.environ['ZIG_PROGRESS'] == original_env
                    for fd in created:
                        try:
                            os.fstat(fd)
                        except OSError:
                            pass
                        else:
                            raise AssertionError('descriptor leak')
                def child():
                    with Progress('retried', refresh_rate=None):
                        pass
                wait(fork_worker(task, child))
                for operation in (task.fork,
                                  lambda: task.spawn([sys.executable, '-c', 'pass'])):
                    try:
                        operation()
                    except ValueError:
                        pass
                    else:
                        raise AssertionError('duplicate attachment')
        """)
        self.assertEqual(trees[-1][1], (0, 0, b"retried", 0))

    def test_fork_at_root_keeps_local_children(self):
        trees = self.run_program("""
            with Progress('parent', refresh_rate=None) as p:
                p.root.start('local')
                def child():
                    with Progress('replacement', 3, refresh_rate=None) as c:
                        c.root.complete_one()
                        c.root.start('remote')
                wait(fork_worker(p.root, child))
        """)
        self.assertEqual(
            trees[-1],
            [(1, 3, b"replacement", 255), (0, 0, b"local", 0), (0, 0, b"remote", 0)],
        )

    def test_nonzero_and_signal_exits_keep_last_report_until_end(self):
        trees = self.run_program("""
            import signal
            with Progress('parent', 2, refresh_rate=None) as p:
                for signaled in (False, True):
                    task = p.root.start('task')
                    pid = task.fork()
                    if pid == 0:
                        with Progress('last report', 4, refresh_rate=None) as c:
                            c.root.set_completed_items(2)
                        if signaled:
                            os.kill(os.getpid(), signal.SIGTERM)
                        os._exit(7)
                    _, status = os.waitpid(pid, 0)
                    expected = -signal.SIGTERM if signaled else 7
                    assert os.waitstatus_to_exitcode(status) == expected
                    assert p.flush()
                    task.end()
        """)
        self.assertIn([(0, 2, b"parent", 255), (2, 4, b"last report", 0)], trees)
        self.assertIn([(1, 2, b"parent", 255), (2, 4, b"last report", 0)], trees)
        self.assertEqual(trees[-1], [(2, 2, b"parent", 255)])

    def test_child_that_never_reports_keeps_task_label(self):
        trees = self.run_program("""
            with Progress('parent', refresh_rate=None) as p:
                task = p.root.start('silent', 5)
                wait(fork_worker(task, lambda: None))
                assert p.flush()
                task.end()
        """)
        self.assertEqual(trees[0], [(0, 0, b"parent", 255), (0, 5, b"silent", 0)])
        self.assertEqual(trees[-1], [(1, 0, b"parent", 255)])

    def test_closing_parent_does_not_kill_child(self):
        trees = self.run_program("""
            read_fd, write_fd = os.pipe()
            with Progress('parent', refresh_rate=None) as p:
                def child():
                    os.close(write_fd)
                    assert os.read(read_fd, 1) == b'x'
                    with Progress('child', refresh_rate=None) as c:
                        # The parent deliberately closed this progress reader.
                        assert not c.flush()
                        assert not c.enabled
                    os.close(read_fd)
                pid = fork_worker(p.root.start('task'), child)
                os.close(read_fd)
                p.close()
                os.write(write_fd, b'x')
                os.close(write_fd)
                wait(pid)
        """)
        self.assertEqual(trees[-1], [(0, 0, b"parent", 255), (0, 0, b"task", 0)])

    def test_repeated_forks_release_descriptors_and_node_capacity(self):
        trees = self.run_program("""
            with Progress('parent', 25, refresh_rate=None) as p:
                baseline = (len(os.listdir('/proc/self/fd'))
                            if os.path.isdir('/proc/self/fd') else None)
                for i in range(25):
                    task = p.root.start('slot')
                    def child():
                        with Progress('child', refresh_rate=None):
                            pass
                    wait(fork_worker(task, child))
                    task.end()
                    assert len(p._nodes) == 1
                    assert not p._children
                    if baseline is not None:
                        assert len(os.listdir('/proc/self/fd')) == baseline
        """)
        self.assertEqual(trees[-1], [(25, 25, b"parent", 255)])

    def test_concurrent_forks_and_reporter_creation(self):
        trees = self.run_program("""
            import threading
            failures = []
            barrier = threading.Barrier(4)
            with Progress('parent', refresh_rate=None) as p:
                def run(index):
                    try:
                        barrier.wait()
                        for iteration in range(3):
                            with Progress(disable=True):
                                task = p.root.start(f'slot-{index}-{iteration}')
                                def child():
                                    with Progress(f'child-{index}-{iteration}',
                                                  refresh_rate=None):
                                        pass
                                wait(fork_worker(task, child))
                    except BaseException as error:
                        failures.append(error)
                threads = [threading.Thread(target=run, args=(i,)) for i in range(4)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
                assert not failures, failures
        """)
        self.assertEqual(len(trees[-1]), 13)
        self.assertEqual(
            {node[2] for node in trees[-1][1:]},
            {f"child-{i}-{j}".encode() for i in range(4) for j in range(3)},
        )
        self.assertTrue(all(node[3] == 0 for node in trees[-1][1:]))

    def test_fork_rejected_after_spawn_and_succeeds_on_new_node(self):
        trees = self.run_program("""
            with Progress('parent', refresh_rate=None) as p:
                task = p.root.start('spawned')
                proc = task.spawn([sys.executable, '-c', 'pass'])
                assert proc.wait(timeout=5) == 0
                try:
                    task.fork()
                except ValueError:
                    pass
                else:
                    raise AssertionError('duplicate attachment accepted')
                task.end()
                def child():
                    with Progress('new child', refresh_rate=None):
                        pass
                wait(fork_worker(p.root.start('new slot'), child))
        """)
        self.assertEqual(trees[-1], [(1, 0, b"parent", 255), (0, 0, b"new child", 0)])


if __name__ == "__main__":
    unittest.main()
