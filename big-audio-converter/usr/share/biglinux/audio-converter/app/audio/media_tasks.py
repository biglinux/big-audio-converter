"""A bounded, single-worker media-probe service with stable request identities."""

from collections import deque
import threading
from app.utils.main_context import SourceGroup
from .media_probe import probe_media
from .process_runner import OperationCancelled, ProcessRunner


class MediaTasks:
    """Do not read GTK widgets or mutable queue positions from the worker."""

    def __init__(self, ffmpeg):
        self.ffmpeg = ffmpeg or "ffmpeg"
        self._condition = threading.Condition()
        self._pending = deque()
        self._callbacks = {}
        self._sources = SourceGroup()
        self._active = None
        self._runner = None
        self._worker = None
        self.closed = False

    def submit(self, identity, source, callback):
        with self._condition:
            if self.closed:
                return False
            if len(self._callbacks) >= 4096:
                raise ValueError("Too many files are waiting for media analysis")
            if identity in self._callbacks:
                raise ValueError("A media request identity must be unique")
            self._callbacks[identity] = callback
            self._pending.append((identity, source))
            if self._worker is None:
                self._worker = threading.Thread(target=self._work, name="bac-media-probe", daemon=True)
                self._worker.start()
            self._condition.notify_all()
        return True

    def _work(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self.closed or self._pending)
                if self.closed:
                    return
                identity, source = self._pending.popleft()
                if identity not in self._callbacks:
                    continue
                runner = ProcessRunner()
                self._active, self._runner = identity, runner
            info, error = None, None
            try:
                info = probe_media(source.path, self.ffmpeg, runner)
            except OperationCancelled:
                pass
            except Exception as exception:
                error = str(exception)
            finally:
                with self._condition:
                    self._active, self._runner = None, None
                    if not self.closed and identity in self._callbacks:
                        self._sources.idle(self._deliver, identity, info, error)

    def _deliver(self, identity, info, error):
        with self._condition:
            callback = self._callbacks.pop(identity, None)
            if self.closed:
                return False
        if callback is not None:
            callback(info, error)
        return False

    def cancel(self, identity):
        with self._condition:
            self._callbacks.pop(identity, None)
            self._pending = deque(item for item in self._pending if item[0] != identity)
            if self._active == identity and self._runner is not None:
                self._runner.cancel()

    def close(self):
        with self._condition:
            self.closed = True
            self._callbacks.clear()
            self._pending.clear()
            if self._runner is not None:
                self._runner.cancel()
            self._condition.notify_all()
        self._sources.close()
        if self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join(timeout=2)
            if self._worker.is_alive():
                raise RuntimeError("Media analysis did not acknowledge shutdown")
