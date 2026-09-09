"""Lazy libmpv playback with explicit file identity and main-loop ownership."""

import gettext
import logging
import math
import os
from copy import deepcopy
from pathlib import Path

from app.utils.main_loop import MainLoopSources

from .media import MediaSource
from .process import MediaError
from .profiles import build_audio_filters, validate_settings

logger = logging.getLogger(__name__)
EQ_FREQUENCIES = (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)


class AudioPlayer:
    """All public methods and application state belong to the GTK thread.

    Native callbacks only copy event data and enqueue owned one-shot sources.
    Playlist entry IDs reject events from replaced files. FFprobe stream
    indices are resolved through libmpv's track-list/ff-index after loading,
    never used directly as mpv audio track IDs.
    """

    def __init__(self, gtcrn_ladspa_path=None, mpv_options=None):
        self.gtcrn_ladspa_path = gtcrn_ladspa_path if gtcrn_ladspa_path and os.path.isfile(gtcrn_ladspa_path) else None
        self.current_file = None
        self.current_actual_file = None
        self.current_track_metadata = None
        self.pending_track_index = None
        self._metadata = {}
        self.duration = 0.0
        self._position = 0.0
        self.is_playing_flag = False
        self._want_play = False
        self._loaded = False
        self._load_requested = False
        self._eof_reached = False
        self._generation = 0
        self._entry_id = None
        self._native_entry_id = None
        self._closed = False
        self.volume = 1.0
        self.speed = 1.0
        self.effects_bypassed = False
        self.pitch_correction = True
        self.noise_reduction = False
        self.noise_strength = 1.0
        self.noise_model = 0
        self.noise_speech_strength = 1.0
        self.noise_lookahead = 0
        self.noise_voice_enhance = 0.0
        self.noise_model_blend = False
        self.hpf_enabled = False
        self.hpf_frequency = 80
        self.transient_enabled = False
        self.transient_attack = -0.5
        self.gate_enabled = False
        self.gate_intensity = 0.5
        self.compressor_enabled = False
        self.compressor_intensity = 1.0
        self.normalize = False
        self.prevent_clipping = False
        self.equalizer_settings = []
        self.position_callback = None
        self.state_callback = None
        self.error_callback = None
        self.duration_callback = None
        self.eos_callback = None
        self.mpv_instance = None
        self.initialization_error = None
        self._mpv_options = dict(mpv_options or {})
        self._sources = MainLoopSources()
        self._last_filter_graph = None
        self.pending_seek_position = None

    def _create_player(self):
        if self._closed:
            return False
        if self.mpv_instance is not None:
            return True
        try:
            import mpv
            options = {"vo": "null", "vid": "no", "sid": "no", "audio_display": "no", "idle": True,
                           "pause": True, "config": False, "load_scripts": False, "ytdl": False,
                           "demuxer": "lavf", "demuxer_lavf_o": "protocol_whitelist=file", "demuxer_max_bytes": "8M", "cache": "no",
                           "replaygain": "no", "volume": 100, "loglevel": "warn", "log_handler": self._log_handler}
            options.update(self._mpv_options)
            self.mpv_instance = mpv.MPV(**options)
            self.mpv_instance.speed = 1.0 if self.effects_bypassed else self.speed
            self.mpv_instance.audio_pitch_correction = self.pitch_correction

            @self.mpv_instance.event_callback("start-file")
            def starting(event):
                self._native_entry_id = int(event.data.playlist_entry_id)

            @self.mpv_instance.event_callback("file-loaded")
            def loaded(event):
                self._sources.idle(self._file_loaded, self._native_entry_id)

            @self.mpv_instance.event_callback("end-file")
            def ended(event):
                data = event.data
                # Copy ctypes-backed fields while the event is still valid.
                self._sources.idle(self._file_ended, int(data.playlist_entry_id), int(data.reason))

            @self.mpv_instance.property_observer("time-pos")
            def position_changed(name, value):
                self._sources.later(33, self._position_changed, self._native_entry_id, value, key="position")

            @self.mpv_instance.property_observer("pause")
            def pause_changed(name, value):
                self._sources.idle(self._pause_changed, self._native_entry_id, value, key="pause")

            self._apply_filters()
            return True
        except (ImportError, OSError, RuntimeError, ValueError, AttributeError) as exc:
            self.initialization_error = str(exc)
            logger.debug("Could not initialize libmpv", exc_info=True)
            if self.mpv_instance is not None:
                self.mpv_instance.terminate()
                self.mpv_instance = None
            self._error(gettext.gettext('Audio preview is unavailable. Install mpv and python-mpv, then reopen the application.'))
            return False

    def _current(self, entry):
        return not self._closed and entry is not None and entry == self._entry_id

    def _file_loaded(self, entry):
        if not self._current(entry) or self.mpv_instance is None:
            return
        try:
            tracks = [track for track in self.mpv_instance.track_list if track.get("type") == "audio"]
            if self.pending_track_index is not None:
                tracks = [track for track in tracks if track.get("ff-index") == self.pending_track_index]
            if not tracks:
                raise MediaError(gettext.gettext('The selected audio track is unavailable. Add the file again.'))
            self.mpv_instance.aid = tracks[0]["id"]
            self._loaded = True
            duration = self.mpv_instance.duration
            self.duration = float(duration) if duration and math.isfinite(duration) else 0.0
            self._apply_filters()
            if self.pending_seek_position is not None:
                self._seek_now(self._generation)
            self.mpv_instance.pause = not self._want_play
            self._notify("duration_callback", self, self.duration)
            self._position_changed(entry, self.mpv_instance.time_pos or 0.0)
            self._set_playing(self._want_play)
        except (OSError, RuntimeError, ValueError, TypeError, MediaError) as exc:
            self.stop()
            self._error(str(exc))

    def _file_ended(self, entry, reason):
        if not self._current(entry):
            return
        self._loaded = False
        self._load_requested = False
        self._want_play = False
        self._set_playing(False)
        if reason == 0:  # MPV_END_FILE_REASON_EOF
            self._eof_reached = True
            self._notify("eos_callback", self)
        elif reason == 4:  # MPV_END_FILE_REASON_ERROR
            self._error(gettext.gettext('The audio could not be played. Check the file and audio output device.'))

    def _position_changed(self, entry, position):
        if not self._current(entry) or not self._loaded or position is None:
            return
        position = float(position)
        if math.isfinite(position):
            self._position = max(0.0, position)
            self._notify("position_callback", self, self._position, self.duration)

    def _pause_changed(self, entry, paused):
        if self._current(entry) and self._loaded and paused is not None:
            self._set_playing(not paused)

    def _notify(self, attribute, *args):
        callback = getattr(self, attribute)
        if not self._closed and callback:
            callback(*args)

    def _set_playing(self, playing):
        changed = self.is_playing_flag != bool(playing)
        self.is_playing_flag = bool(playing)
        if changed:
            self._notify("state_callback", self, self.is_playing_flag)

    def _error(self, message):
        self._notify("error_callback", message)

    def _log_handler(self, level, component, message):
        # Native diagnostics can contain paths and metadata. Keep them in
        # explicit debug output; end-file errors get a separate UI message.
        logger.debug("libmpv %s [%s]: %s", level, component, message.rstrip())

    def load(self, file_path, track_metadata=None):
        if self._closed or not self._create_player() or self.mpv_instance is None:
            return False
        try:
            source = MediaSource.resolve(file_path, track_metadata)
            self._generation += 1
            self._sources.clear()
            self._entry_id = None
            self._loaded = False
            self._load_requested = True
            self._want_play = False
            self._set_playing(False)
            self._eof_reached = False
            self.pending_seek_position = None
            self.current_file = file_path
            self.current_actual_file = source.path
            self._metadata = deepcopy(track_metadata or {})
            self.current_track_metadata = self._metadata.get(file_path)
            self.pending_track_index = source.stream_index
            self.duration = 0.0
            self._position = 0.0
            self.mpv_instance.pause = True
            self.mpv_instance.command("loadfile", source.path, "replace")
            # loadfile changes the playlist synchronously but loads the file
            # asynchronously. Main-loop delivery cannot run until this method
            # has captured the new playlist entry's lifetime-unique ID.
            playlist = self.mpv_instance.playlist
            self._entry_id = playlist[0]["id"] if playlist else None
            return True
        except (OSError, RuntimeError, ValueError, MediaError) as exc:
            self._load_requested = False
            self._error(str(exc))
            return False

    def play(self):
        if self._closed or not self.current_file or self.mpv_instance is None:
            return False
        if (not self._load_requested and not self._loaded) and (not self.load(self.current_file, self._metadata)):
            return False
        self._want_play = True
        if self._loaded:
            self.mpv_instance.pause = False
        self._set_playing(True)
        return True

    def pause(self):
        self._want_play = False
        if self.mpv_instance:
            self.mpv_instance.pause = True
        self._set_playing(False)

    def stop(self):
        self._generation += 1
        self._sources.clear()
        self._entry_id = None
        self._want_play = False
        self._loaded = False
        self._load_requested = False
        self.pending_seek_position = None
        if self.mpv_instance and not self._closed:
            self.mpv_instance.command("stop")
        self._position = 0.0
        self._set_playing(False)

    def seek(self, position):
        position = float(position)
        if not math.isfinite(position) or position < 0:
            raise ValueError("Seek position must be finite and nonnegative")
        if self.duration > 0:
            position = min(position, self.duration)
        self.pending_seek_position = position
        if self._loaded:
            self._sources.later(30, self._seek_now, self._generation, key="seek")
        return True

    def _seek_now(self, generation):
        if self._closed or generation != self._generation or not self._loaded or self.mpv_instance is None:
            return
        position, self.pending_seek_position = self.pending_seek_position, None
        if position is not None:
            try:
                self.mpv_instance.command("seek", f"{position:.9f}", "absolute+exact")
                self._position = position
            except (RuntimeError, ValueError) as exc:
                logger.debug("Seek failed: %s", exc)
                self._error(gettext.gettext('This audio position could not be reached.'))

    def _effect_settings(self):
        fields = ("noise_reduction", "noise_strength", "noise_model", "noise_speech_strength", "noise_lookahead",
                  "noise_voice_enhance", "noise_model_blend", "hpf_enabled", "hpf_frequency", "transient_enabled",
                  "transient_attack", "gate_enabled", "gate_intensity", "compressor_enabled", "compressor_intensity",
                  "volume", "normalize", "prevent_clipping")
        settings = {field: getattr(self, field) for field in fields}
        gains = dict(self.equalizer_settings)
        settings.update(eq_enabled=bool(gains), eq_bands=",".join(str(gains.get(frequency, 0)) for frequency in EQ_FREQUENCIES))
        return settings

    def _apply_filters(self):
        if self._closed or self.mpv_instance is None:
            return
        try:
            graph = "" if self.effects_bypassed else ",".join(build_audio_filters(self._effect_settings(), self.gtcrn_ladspa_path))
            if graph != self._last_filter_graph:
                self.mpv_instance.af = f"lavfi=[{graph}]" if graph else ""
                self._last_filter_graph = graph
        except (MediaError, RuntimeError, ValueError) as exc:
            logger.debug("Preview filter failure: %s", exc)
            self._error(gettext.gettext('A preview effect could not be applied. Check the selected effects and installed plugins.'))

    def _rebuild_audio_filters(self):
        if self.mpv_instance and not self._closed:
            self._sources.later(40, self._apply_filters, key="filters")

    def _set_effect(self, name, value):
        settings = self._effect_settings()
        settings[name] = value
        validate_settings(settings)
        setattr(self, name, value)
        self._rebuild_audio_filters()

    def set_effects_bypassed(self, bypassed):
        self.effects_bypassed = bool(bypassed)
        if self.mpv_instance:
            self.mpv_instance.speed = 1.0 if self.effects_bypassed else self.speed
        self._rebuild_audio_filters()

    def set_volume(self, volume):
        # mpv's volume percentage is perceptual, not a linear amplitude factor.
        # Use the same lavfi volume stage as export, with native volume at 100.
        self._set_effect("volume", float(volume))

    def set_playback_speed(self, speed):
        speed = validate_settings({"speed": speed})["speed"]
        self.speed = speed
        if self.mpv_instance:
            self.mpv_instance.speed = 1.0 if self.effects_bypassed else speed

    def set_pitch_correction(self, enabled):
        self.pitch_correction = bool(enabled)
        if self.mpv_instance:
            self.mpv_instance.audio_pitch_correction = self.pitch_correction

    def set_equalizer_bands(self, bands):
        if any(frequency not in EQ_FREQUENCIES for frequency, _ in bands):
            raise ValueError("Unsupported equalizer frequency")
        settings = self._effect_settings()
        settings.update(eq_enabled=True, eq_bands=",".join(str(dict(bands).get(f, 0)) for f in EQ_FREQUENCIES))
        validate_settings(settings)
        self.equalizer_settings = list(bands)
        self._rebuild_audio_filters()

    def set_noise_reduction(self, enabled):
        if enabled and not self.gtcrn_ladspa_path:
            self._error(gettext.gettext('Noise reduction requires the GTCRN plugin.'))
            return False
        self._set_effect("noise_reduction", bool(enabled))
        return True

    def set_noise_strength(self, value):
        self._set_effect("noise_strength", float(value))

    def set_noise_model(self, value):
        self._set_effect("noise_model", int(value))

    def set_noise_advanced(self, speech_strength=1.0, lookahead=0, voice_enhance=0.0, model_blend=False):
        values = {"noise_speech_strength": speech_strength, "noise_lookahead": lookahead,
                      "noise_voice_enhance": voice_enhance, "noise_model_blend": bool(model_blend)}
        validate_settings(self._effect_settings() | values)
        for key, value in values.items():
            setattr(self, key, value)
        self._rebuild_audio_filters()

    def set_hpf_enabled(self, enabled):
        self._set_effect("hpf_enabled", bool(enabled))

    def set_hpf_frequency(self, value):
        self._set_effect("hpf_frequency", int(value))

    def set_transient_enabled(self, enabled):
        path = Path(self.gtcrn_ladspa_path).parent / "transient_split.so" if self.gtcrn_ladspa_path else None
        if enabled and (path is None or not path.is_file()):
            self._error(gettext.gettext('Transient suppression requires its LADSPA plugin.'))
            return False
        self._set_effect("transient_enabled", bool(enabled))
        return True

    def set_transient_attack(self, value):
        self._set_effect("transient_attack", float(value))

    def set_gate_enabled(self, enabled):
        self._set_effect("gate_enabled", bool(enabled))

    def set_gate_intensity(self, value):
        self._set_effect("gate_intensity", float(value))

    def set_compressor_enabled(self, enabled):
        self._set_effect("compressor_enabled", bool(enabled))

    def set_compressor_intensity(self, value):
        self._set_effect("compressor_intensity", float(value))

    def set_normalize(self, enabled):
        self._set_effect("normalize", bool(enabled))

    def set_prevent_clipping(self, enabled):
        self._set_effect("prevent_clipping", bool(enabled))

    def is_playing(self):
        return self.is_playing_flag

    def connect(self, signal_name, callback):
        attribute = {"position-updated": "position_callback", "state-changed": "state_callback", "error": "error_callback",
                     "duration-changed": "duration_callback", "eos": "eos_callback"}.get(signal_name)
        if attribute is None:
            raise ValueError(f"Unknown player signal: {signal_name}")
        setattr(self, attribute, callback)

    def cleanup(self):
        if self._closed:
            return
        self.stop()
        self._closed = True
        self._sources.close()
        for attribute in ("position_callback", "state_callback", "error_callback", "duration_callback", "eos_callback"):
            setattr(self, attribute, None)
        instance, self.mpv_instance = self.mpv_instance, None
        if instance is not None:
            # python-mpv terminates and joins its event thread here. Never call
            # this from a native event callback (only from the GTK owner).
            instance.terminate()
