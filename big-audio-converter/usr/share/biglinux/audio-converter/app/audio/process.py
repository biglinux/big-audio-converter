"""Bounded, cancellable subprocess execution without a GTK dependency."""

import gettext
import os
import selectors
import signal
import subprocess
import threading
import time
from dataclasses import dataclass


class CancelledError(Exception):
    """The owner cancelled the operation."""


class MediaError(Exception):
    """An actionable failure with separate, bounded diagnostic details."""

    def __init__(self, message, details=""):
        super().__init__(message)
        self.details = details


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Own at most one process group and always drain and reap its process.

    Run in a worker. ``cancel`` is nonblocking and can be called by the UI.
    A runner is an operation token: create a new runner for a new operation;
    never clear cancellation on an operation which may still be running.
    """

    DIAGNOSTIC_LIMIT = 65536
    TERMINATE_GRACE = 0.5

    def __init__(self):
        self.cancelled = threading.Event()
        self._lock = threading.Lock()
        self._process = None

    @property
    def process(self):
        with self._lock:
            return self._process

    def check_cancelled(self):
        if self.cancelled.is_set():
            raise CancelledError()

    @staticmethod
    def _signal(process, sig):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass

    def cancel(self):
        self.cancelled.set()
        with self._lock:
            if self._process is not None:
                self._signal(self._process, signal.SIGTERM)

    def run(self, argv, *, timeout=None, stdout_callback=None, output_limit=8 * 1024 * 1024):
        """Consume both pipes independently of progress or known duration.

        Streaming callers receive arbitrary byte chunks, not complete frames.
        Captured stdout is capped; stderr retains only its diagnostic tail.
        A timeout or callback exception terminates the entire owned group.
        """
        with self._lock:
            self.check_cancelled()
            if self._process is not None:
                raise RuntimeError("A process runner cannot execute concurrent commands")
            process = subprocess.Popen(
                list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True, bufsize=0,
            )
            self._process = process
        # PIPE guarantees both descriptors; retain the invariant for type checking.
        assert process.stdout is not None and process.stderr is not None
        output = bytearray()
        errors = bytearray()
        deadline = time.monotonic() + timeout if timeout is not None else None
        completed = False
        try:
            with selectors.DefaultSelector() as selector:
                for stream in (process.stdout, process.stderr):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ)
                while selector.get_map():
                    self.check_cancelled()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise MediaError(gettext.gettext('The media command timed out.'), "Command exceeded its deadline")
                    for key, _ in selector.select(0.05):
                        data = os.read(key.fd, 65536)
                        if not data:
                            selector.unregister(key.fileobj)
                            continue
                        if key.fileobj is process.stderr:
                            errors.extend(data)
                            if len(errors) > self.DIAGNOSTIC_LIMIT:
                                del errors[:-self.DIAGNOSTIC_LIMIT]
                        elif stdout_callback is not None:
                            stdout_callback(data)
                        else:
                            if len(output) + len(data) > output_limit:
                                raise MediaError(gettext.gettext('The media information is too large to process safely.'))
                            output.extend(data)
                # The process may close its pipes before it exits. Still honour
                # cancellation/deadlines rather than blocking in wait().
                while process.poll() is None:
                    self.check_cancelled()
                    if deadline is not None and time.monotonic() >= deadline:
                        raise MediaError(gettext.gettext('The media command timed out.'))
                    self.cancelled.wait(0.02)
                self.check_cancelled()
                result = ProcessResult(
                    process.returncode, output.decode("utf-8", errors="replace"),
                    errors.decode("utf-8", errors="replace"),
                )
                completed = True
                return result
        finally:
            if not completed:
                self._signal(process, signal.SIGTERM)
                try:
                    process.wait(timeout=self.TERMINATE_GRACE)
                except subprocess.TimeoutExpired:
                    pass
                # A wrapper may have exited while a grandchild ignores TERM.
                self._signal(process, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()
            with self._lock:
                if self._process is process:
                    self._process = None
