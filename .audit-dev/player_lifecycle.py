"""Native mpv event delivery and deterministic player resource ownership."""

from editing import ROOT, method, source_method


def apply():
    path = ROOT / "app/audio/player.py"
    text = path.read_text()
    text = text.replace('import mpv\n', 'try:\n    import mpv\nexcept (ImportError, OSError):\n    mpv = None\n')
    if 'from app.utils.main_context import SourceGroup' not in text:
        text = text.replace('from gi.repository import GLib\n', 'from gi.repository import GLib\nfrom app.utils.main_context import SourceGroup\nfrom .models import MediaSource, finite_number\n')
    path.write_text(text)
    init = source_method(path, "AudioPlayer", "__init__")
    init = init.replace('def __init__(self, gtcrn_ladspa_path=None):', 'def __init__(self, gtcrn_ladspa_path=None, audio_output=None):')
    init = init.replace('    self._create_player()', '''    self._sources = SourceGroup()
    self._events = SourceGroup()
    self._generation = 0
    self._disposed = False
    self._loaded = False
    self._expected_entry = None
    self._audio_output = audio_output
    self.initialization_error = None
    self.normalize_enabled = False
    self.bypass_processing = False
    self._last_filter_chain = None
    self._create_player()''')
    method(path, "AudioPlayer", "__init__", init)
    method(path, "AudioPlayer", "_create_player", '''
    def _create_player(self):
        """Create an audio-only player without loading user scripts or network helpers."""
        try:
            if mpv is None:
                raise RuntimeError("The native mpv library or Python binding is missing")
            options = dict(vo="null", vid="no", sid="no", audio_display="no",
                           config=False, load_scripts=False, ytdl=False,
                           input_default_bindings=False, demuxer="lavf", keep_open="yes",
                           volume_max=1000, cache="yes", demuxer_max_bytes="50M",
                           log_handler=self._log_handler, loglevel="warn")
            if self._audio_output is not None:
                options["ao"] = self._audio_output
            self.mpv_instance = mpv.MPV(**options)
            @self.mpv_instance.event_callback("file-loaded")
            def loaded(event):
                self._post_event(self._file_loaded)
            @self.mpv_instance.event_callback("end-file")
            def ended(event):
                data = getattr(event, "data", None)
                reason = getattr(data, "reason", -1)
                identity = getattr(data, "playlist_entry_id", None)
                if isinstance(event, dict):
                    data = event.get("event", event)
                    reason = data.get("reason", -1)
                    identity = data.get("playlist_entry_id")
                self._post_event(self._file_ended, reason, identity)
            self._rebuild_audio_filters()
        except Exception as error:
            self.initialization_error = str(error)
            logger.warning("Audio preview is unavailable: %s", error)
            instance, self.mpv_instance = self.mpv_instance, None
            if instance is not None:
                instance.terminate()
    ''')
    method(path, "AudioPlayer", "_post_event", '''
    def _post_event(self, callback, *args):
        generation = self._generation
        def deliver():
            if not self._disposed and generation == self._generation:
                callback(*args)
            return False
        self._events.idle(deliver)
    ''')
    method(path, "AudioPlayer", "_emit", '''
    def _emit(self, name, *args):
        if self._disposed:
            return
        callback = getattr(self, name + "_callback", None)
        if callback is not None:
            callback(self, *args)
    ''')
    method(path, "AudioPlayer", "_file_loaded", '''
    def _file_loaded(self):
        if self._disposed or self.mpv_instance is None or not self.current_actual_file:
            return
        try:
            if self.mpv_instance.path != self.current_actual_file:
                return
            tracks = [track for track in self.mpv_instance.track_list if track.get("type") == "audio"]
            if self.pending_track_index is not None:
                tracks = [track for track in tracks if track.get("ff-index") == self.pending_track_index]
            if not tracks:
                raise ValueError("The selected audio stream is unavailable")
            selected = min(tracks, key=lambda track: track.get("ff-index", track["id"]))
            self.mpv_instance.aid = selected["id"]
            self._loaded = True
            self.duration = float(self.mpv_instance.duration or 0)
            self._emit("duration", self.duration)
            self._emit("position", self._position, self.duration)
            if self.pending_seek_position is not None:
                position = self.pending_seek_position
                self.pending_seek_position = None
                self._do_seek(position)
            self.mpv_instance.pause = not self.is_playing_flag
        except Exception as error:
            self.pause()
            self._emit("error", str(error))
    ''')
    method(path, "AudioPlayer", "_file_ended", '''
    def _file_ended(self, reason, identity):
        if identity is not None and self._expected_entry is not None and identity != self._expected_entry:
            return
        if reason in (0, "eof"):
            self._reached_end()
        elif reason in (4, "error"):
            self.pause()
            self._emit("error", _("Audio playback failed. Check the file and audio output device."))
    ''')
    method(path, "AudioPlayer", "_reached_end", '''
    def _reached_end(self):
        if self._eof_reached or not self._loaded:
            return
        self._eof_reached = True
        self.is_playing_flag = False
        self._stop_position_timer()
        self._emit("state", False)
        self._emit("eos")
    ''')
    method(path, "AudioPlayer", "_log_handler", '''
    def _log_handler(self, level, component, message):
        # Native decoder details are diagnostics, not untranslated GUI errors.
        if level == "error":
            logger.debug("mpv %s: %s", component, message.rstrip())
    ''')
    method(path, "AudioPlayer", "_stop_position_timer", '''
    def _stop_position_timer(self):
        self._sources.remove(self.position_timer_id)
        self.position_timer_id = None
    ''')
    method(path, "AudioPlayer", "_position_update_callback", '''
    def _position_update_callback(self):
        if self._disposed or not self.is_playing_flag or self.mpv_instance is None:
            self.position_timer_id = None
            return False
        if not self._loaded:
            return True
        try:
            position = self.mpv_instance.time_pos
            if position is not None:
                self._position = position
                self._emit("position", position, self.duration)
            if self.mpv_instance.eof_reached:
                self._reached_end()
                return False
        except Exception as error:
            self.pause()
            self._emit("error", str(error))
            return False
        return True
    ''')
    method(path, "AudioPlayer", "cleanup", '''
    def cleanup(self):
        """Idempotently stop callbacks before terminating and joining native mpv."""
        if getattr(self, "_disposed", True):
            return
        self._disposed = True
        self._generation += 1
        self.is_playing_flag = False
        self._events.close()
        self._sources.close()
        self.position_timer_id = None
        self.seek_timer_id = None
        self.pending_seek_position = None
        for name in ("position", "state", "error", "duration", "eos"):
            setattr(self, name + "_callback", None)
        instance, self.mpv_instance = self.mpv_instance, None
        if instance is not None:
            instance.terminate()
    ''')
    method(path, "AudioPlayer", "__del__", '''
    def __del__(self):
        # Application shutdown explicitly calls cleanup on the main thread.
        # Never join an mpv event thread from an arbitrary finalizer thread.
        pass
    ''')
