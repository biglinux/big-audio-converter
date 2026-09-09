"""Lifecycle-managed GLib callbacks for the desktop application."""

import logging
import threading
from gi.repository import GLib

logger = logging.getLogger(__name__)


class SourceGroup:
    """Own GLib source IDs and release them when their UI owner closes."""

    def __init__(self):
        self._lock = threading.RLock()
        self._sources = set()
        self._closed = False

    def _add(self, delay, callback, args, repeat):
        identity = [None]
        def invoke():
            with self._lock:
                if self._closed:
                    self._sources.discard(identity[0])
                    return False
            again = False
            try:
                result = callback(*args)
                again = repeat and bool(result)
                return again
            except Exception:
                logger.exception("Main-context callback failed")
                return False
            finally:
                if not again:
                    with self._lock:
                        self._sources.discard(identity[0])
        with self._lock:
            if self._closed:
                return None
            source = GLib.idle_add(invoke) if delay is None else GLib.timeout_add(delay, invoke)
            identity[0] = source
            self._sources.add(source)
            return source

    def idle(self, callback, *args):
        return self._add(None, callback, args, False)

    def timeout(self, milliseconds, callback, *args):
        return self._add(milliseconds, callback, args, True)

    def remove(self, identity):
        if identity is None:
            return
        with self._lock:
            self._sources.discard(identity)
            if GLib.MainContext.default().find_source_by_id(identity) is not None:
                GLib.source_remove(identity)

    def close(self):
        with self._lock:
            self._closed = True
            for identity in tuple(self._sources):
                self.remove(identity)
            self._sources.clear()
