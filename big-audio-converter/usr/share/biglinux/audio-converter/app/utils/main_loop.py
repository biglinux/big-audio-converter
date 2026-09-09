"""Explicit ownership of one-shot GLib sources with optional coalescing."""

import threading

from gi.repository import GLib


class MainLoopSources:
    """Marshal worker results to GTK and invalidate every callback on close.

    A coalescing key retains only the most recent callback/arguments for that
    source. Callback return values never accidentally create repeating idles.
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._sources = {}
        self._keys = {}
        self.closed = False

    def idle(self, callback, *args, key=None):
        return self.later(0, callback, *args, key=key)

    def later(self, milliseconds, callback, *args, key=None):
        with self._lock:
            if self.closed:
                return None
            if key is not None and key in self._keys:
                identifier = self._keys[key]
                self._sources[identifier] = (callback, args, key)
                return identifier
            source = GLib.idle_source_new() if milliseconds == 0 else GLib.timeout_source_new(milliseconds)
            holder: list[int] = []

            def deliver(*unused):
                with self._lock:
                    item = self._sources.pop(holder[0], None)
                    if item is not None and item[2] is not None:
                        self._keys.pop(item[2], None)
                    active = not self.closed
                if active and item:
                    item[0](*item[1])
                return GLib.SOURCE_REMOVE

            source.set_callback(deliver)
            identifier = source.attach(GLib.MainContext.default())
            holder.append(identifier)
            self._sources[identifier] = (callback, args, key)
            if key is not None:
                self._keys[key] = identifier
            return identifier

    def cancel(self, identifier):
        with self._lock:
            item = self._sources.pop(identifier, None)
            if item is not None:
                if item[2] is not None:
                    self._keys.pop(item[2], None)
                GLib.source_remove(identifier)

    def clear(self):
        with self._lock:
            for identifier in self._sources:
                GLib.source_remove(identifier)
            self._sources.clear()
            self._keys.clear()

    def close(self):
        with self._lock:
            self.closed = True
            self.clear()
