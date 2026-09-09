"""Bounded asynchronous media inspection shared by queue and information views."""

import gettext
import json
import logging
import os
import stat
import threading
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass

from app.utils.main_loop import MainLoopSources

from .media import probe_media
from .process import CancelledError, MediaError, ProcessRunner


@dataclass(frozen=True)
class ProbeRequest:
    token: int
    path: str
    callback: object


class ProbeService:
    """One worker, bounded pending work, stat-keyed cache, and owned delivery.

    Requests contain paths and tokens, not widgets or mutable queue indices.
    Callbacks execute on the GLib main context and are invalidated on cancel.
    """
    MAX_PENDING = 1024
    CACHE_BYTES = 4 * 1024 * 1024
    CACHE_ENTRIES = 64

    def __init__(self, ffmpeg_path):
        self.ffmpeg_path = ffmpeg_path
        self._condition = threading.Condition()
        self._pending = OrderedDict()
        self._active = set()
        self._next_token = 0
        self._current = None
        self._runner = None
        self._worker = None
        self._closed = False
        self._sources = MainLoopSources()
        self._cache = OrderedDict()
        self._cache_bytes = 0

    def request(self, path, callback, *, priority=False):
        path = os.path.abspath(os.fspath(path))
        with self._condition:
            if self._closed:
                raise RuntimeError("Media inspection has been closed")
            if len(self._pending) >= self.MAX_PENDING:
                raise MediaError(gettext.gettext('Too many files are waiting for inspection. Wait for some files to finish, then add more.'))
            self._next_token += 1
            token = self._next_token
            self._active.add(token)
            self._pending[token] = ProbeRequest(token, path, callback)
            if priority:
                self._pending.move_to_end(token, last=False)
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="media-probe")
                self._worker.start()
            self._condition.notify()
            return token

    @staticmethod
    def _identity(path):
        identity = os.stat(path)
        if not stat.S_ISREG(identity.st_mode):
            raise MediaError(gettext.gettext('Select a regular local media file, not a folder or device.'))
        return (path, identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns)

    def _inspect(self, request, runner):
        key = self._identity(request.path)
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return deepcopy(cached[0])
        info = probe_media(request.path, runner, self.ffmpeg_path)
        if self._identity(request.path) != key:
            raise MediaError(gettext.gettext('The file changed during inspection. Add it again.'))
        size = len(json.dumps(info, ensure_ascii=False).encode("utf-8"))
        if size <= self.CACHE_BYTES:
            self._cache[key] = (deepcopy(info), size)
            self._cache_bytes += size
            while self._cache_bytes > self.CACHE_BYTES or len(self._cache) > self.CACHE_ENTRIES:
                _, (_, evicted_size) = self._cache.popitem(last=False)
                self._cache_bytes -= evicted_size
        return info

    def _run(self):
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._pending))
                if self._closed:
                    return
                token, request = self._pending.popitem(last=False)
                self._current = token
                self._runner = runner = ProcessRunner()
            info, error = None, None
            try:
                info = self._inspect(request, runner)
            except CancelledError:
                pass
            except (MediaError, OSError, ValueError, TypeError) as exc:
                error = str(exc)
            except Exception:
                logging.getLogger(__name__).exception("Unexpected media inspection error")
                error = "The file could not be inspected. Add it again or choose another file."
            finally:
                with self._condition:
                    self._current = None
                    self._runner = None
            self._sources.idle(self._deliver, request, info, error)

    def _deliver(self, request, info, error):
        with self._condition:
            active = not self._closed and request.token in self._active
            self._active.discard(request.token)
        if active:
            request.callback(request.token, request.path, info, error)

    def cancel(self, token):
        with self._condition:
            self._active.discard(token)
            self._pending.pop(token, None)
            if self._current == token and self._runner is not None:
                self._runner.cancel()

    def cleanup(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._active.clear()
            self._pending.clear()
            if self._runner is not None:
                self._runner.cancel()
            self._condition.notify_all()
        self._sources.close()
        if self._worker is not None:
            self._worker.join(timeout=2)
        self._cache.clear()
        self._cache_bytes = 0
