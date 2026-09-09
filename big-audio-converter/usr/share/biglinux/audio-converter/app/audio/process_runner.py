"""Bounded, cancellable subprocess execution, independent of GTK."""

from dataclasses import dataclass
import os
import selectors
import signal
import subprocess
import threading
import time


class OperationCancelled(Exception):
    """The owner cancelled this operation."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: bytes
    stderr: str


class ProcessRunner:
    """Own one process and reap it before releasing ownership.

    Pipes are drained independently of progress reporting. Diagnostics have a
    fixed memory budget. cancel() is nonblocking; run() acknowledges cancellation
    only after the child has terminated and has been reaped.
    """

    def __init__(self, cancelled=None):
        self._cancelled = cancelled or (lambda: False)
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._process = None
        self._interrupted = threading.Event()
        self.diagnostic_limit = 65536

    @property
    def process(self):
        with self._lock:
            return self._process

    @staticmethod
    def _signal(process, sig):
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(self):
        self._interrupted.set()
        with self._lock:
            if self._process is not None:
                self._signal(self._process, signal.SIGTERM)

    def reset(self):
        with self._lock:
            if self._process is not None:
                raise RuntimeError("The previous process has not finished")
            self._interrupted.clear()

    def run(self, command, *, timeout=None, stdout_consumer=None,
            capture_limit=8 * 1024 * 1024):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("Concurrent use of a process runner is not allowed")
        process = None
        stdout = bytearray()
        stderr = bytearray()
        started = time.monotonic()
        try:
            if self._interrupted.is_set() or self._cancelled():
                raise OperationCancelled()
            with self._lock:
                process = subprocess.Popen(
                    list(command), stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True, close_fds=True,
                )
                self._process = process
            with selectors.DefaultSelector() as selector:
                for pipe, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(pipe.fileno(), False)
                    selector.register(pipe, selectors.EVENT_READ, label)
                while selector.get_map():
                    if self._interrupted.is_set() or self._cancelled():
                        raise OperationCancelled()
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(list(command), timeout)
                    for key, _ in selector.select(timeout=0.05):
                        data = os.read(key.fileobj.fileno(), 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        if key.data == "stderr":
                            stderr.extend(data)
                            if len(stderr) > self.diagnostic_limit:
                                del stderr[:-self.diagnostic_limit]
                        elif stdout_consumer is not None:
                            stdout_consumer(data)
                        else:
                            if len(stdout) + len(data) > capture_limit:
                                raise ValueError("Subprocess output exceeds its memory budget")
                            stdout.extend(data)
                # Closing pipes does not necessarily mean the process exited.
                while process.poll() is None:
                    if self._interrupted.is_set() or self._cancelled():
                        raise OperationCancelled()
                    if timeout is not None and time.monotonic() - started > timeout:
                        raise subprocess.TimeoutExpired(list(command), timeout)
                    time.sleep(0.02)
                if self._interrupted.is_set() or self._cancelled():
                    raise OperationCancelled()
                return ProcessResult(process.returncode, bytes(stdout), stderr.decode("utf-8", "replace"))
        finally:
            if process is not None:
                if process.poll() is None:
                    self._signal(process, signal.SIGTERM)
                    try:
                        process.wait(timeout=0.5)
                    except subprocess.TimeoutExpired:
                        self._signal(process, signal.SIGKILL)
                process.wait()
                for pipe in (process.stdout, process.stderr):
                    if pipe is not None:
                        pipe.close()
                with self._lock:
                    if self._process is process:
                        self._process = None
            self._run_lock.release()
