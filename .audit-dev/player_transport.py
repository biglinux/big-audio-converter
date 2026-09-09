"""Player transport changes: stable loads, bounded seeks and linear controls."""

from editing import ROOT, method


def apply():
    path = ROOT / "app/audio/player.py"
    method(path, "AudioPlayer", "load", '''
    def load(self, file_path, track_metadata=None):
        if self._disposed or self.mpv_instance is None:
            self._emit("error", _("Audio preview is unavailable. Install mpv and its Python binding."))
            return False
        try:
            source = MediaSource.resolve(file_path, track_metadata)
            if not os.path.isfile(source.path):
                raise ValueError("Select a readable local media file")
            self._generation += 1
            self._events.close()
            self._events = SourceGroup()
            self._stop_position_timer()
            self._sources.remove(self.seek_timer_id)
            self.seek_timer_id = None
            self.pending_seek_position = None
            self.last_seek_time = 0
            self._loaded = False
            self._eof_reached = False
            self.is_playing_flag = False
            self._position = 0
            self.duration = 0
            self.current_file = file_path
            self.current_actual_file = source.path
            self.current_track_metadata = (track_metadata or {}).get(file_path)
            self.pending_track_index = source.stream_index
            self.mpv_instance.pause = True
            self.mpv_instance.loadfile(source.path, "replace")
            entries = self.mpv_instance.playlist or []
            self._expected_entry = entries[0].get("id") if entries else None
            return True
        except Exception as error:
            self._emit("error", str(error))
            return False
    ''')
    method(path, "AudioPlayer", "play", '''
    def play(self):
        if self._disposed or self.mpv_instance is None or not self.current_file:
            return False
        if self.is_playing_flag:
            return True
        try:
            if self._eof_reached:
                self._do_seek(0)
            self.is_playing_flag = True
            if self._loaded:
                self.mpv_instance.pause = False
            self._emit("state", True)
            if self.position_timer_id is None:
                self.position_timer_id = self._sources.timeout(50, self._position_update_callback)
            return True
        except Exception as error:
            self.is_playing_flag = False
            self._emit("error", str(error))
            return False
    ''')
    method(path, "AudioPlayer", "pause", '''
    def pause(self):
        self.is_playing_flag = False
        self._stop_position_timer()
        if self._disposed or self.mpv_instance is None:
            return False
        try:
            self.mpv_instance.pause = True
            self._emit("state", False)
            return True
        except Exception as error:
            logger.debug("Could not pause audio: %s", error)
            return False
    ''')
    method(path, "AudioPlayer", "stop", '''
    def stop(self):
        if self._disposed:
            return False
        self.pause()
        self._generation += 1
        self._events.close()
        self._events = SourceGroup()
        self._sources.remove(self.seek_timer_id)
        self.seek_timer_id = None
        self.pending_seek_position = None
        self._position = 0
        self._eof_reached = False
        self._loaded = False
        self.current_file = None
        self.current_actual_file = None
        self._expected_entry = None
        if self.mpv_instance is not None:
            self.mpv_instance.command("stop")
        self._emit("position", 0, self.duration)
        return True
    ''')
    method(path, "AudioPlayer", "seek", '''
    def seek(self, position):
        if self._disposed or self.mpv_instance is None:
            return False
        position = finite_number(position, "Seek position", 0, max(self.duration, 1e10))
        if self.duration > 0:
            position = min(position, self.duration)
        if not self._loaded:
            self.pending_seek_position = position
            return True
        now = time.monotonic() * 1000
        delay = self.seek_throttle_ms - (now - self.last_seek_time)
        if delay > 0:
            self.pending_seek_position = position
            if self.seek_timer_id is None:
                self.seek_timer_id = self._sources.timeout(max(1, int(delay)), self._execute_pending_seek)
            return True
        return self._do_seek(position)
    ''')
    method(path, "AudioPlayer", "_execute_pending_seek", '''
    def _execute_pending_seek(self):
        self.seek_timer_id = None
        position, self.pending_seek_position = self.pending_seek_position, None
        if position is not None and not self._disposed:
            self._do_seek(position)
        return False
    ''')
    method(path, "AudioPlayer", "_do_seek", '''
    def _do_seek(self, position):
        if self._disposed or self.mpv_instance is None:
            return False
        self.last_seek_time = time.monotonic() * 1000
        if not self._loaded:
            self.pending_seek_position = position
            return True
        try:
            self.is_seeking = True
            self.mpv_instance.seek(position, reference="absolute", precision="exact")
            self._position = position
            self._eof_reached = False
            self._emit("position", position, self.duration)
            return True
        except Exception as error:
            self._emit("error", str(error))
            return False
        finally:
            self.is_seeking = False
    ''')
    method(path, "AudioPlayer", "_complete_seek", '''
    def _complete_seek(self, position, restore_playing):
        if self._do_seek(position) and restore_playing:
            self.play()
        return False
    ''')
    method(path, "AudioPlayer", "_retry_seek_after_reload", '''
    def _retry_seek_after_reload(self, position, restore_playing):
        return self._complete_seek(position, restore_playing)
    ''')
    method(path, "AudioPlayer", "set_volume", '''
    def set_volume(self, volume):
        """Use the same linear gain as export: 1.0 means original volume."""
        self.volume = finite_number(volume, "Volume", 0, 10)
        if not self._disposed and self.mpv_instance is not None:
            self.mpv_instance.volume = 100 if self.bypass_processing else self.volume * 100
    ''')
    method(path, "AudioPlayer", "set_playback_speed", '''
    def set_playback_speed(self, speed):
        self.speed = finite_number(speed, "Playback speed", 0.1, 5)
        if not self._disposed and self.mpv_instance is not None:
            self.mpv_instance.speed = 1 if self.bypass_processing else self.speed
    ''')
    method(path, "AudioPlayer", "set_pitch_correction", '''
    def set_pitch_correction(self, enabled):
        self.pitch_correction = bool(enabled)
        if self.mpv_instance is not None:
            self.mpv_instance["audio-pitch-correction"] = self.pitch_correction
            self._rebuild_audio_filters()
    ''')
