"""
Audio player module using MPV for playback functionality.
"""

import gettext
import logging
import math
import os
import time
from pathlib import Path

try:
    import mpv
except (ImportError, OSError):
    mpv = None
from gi.repository import GLib
from app.utils.main_context import SourceGroup
from .filters import build_audio_filters
from .models import MediaSource, finite_number

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Audio player using MPV for robust audio playback functionality.
    """

    def __init__(self, gtcrn_ladspa_path=None, audio_output=None):
        """Initialize the audio player with MPV."""
        self.gtcrn_ladspa_path = gtcrn_ladspa_path
        if self.gtcrn_ladspa_path and not os.path.exists(self.gtcrn_ladspa_path):
            logger.warning(f"GTCRN LADSPA plugin not found: {self.gtcrn_ladspa_path}")
            self.gtcrn_ladspa_path = None

        # Initialize playback properties
        self.current_file = None
        self.current_actual_file = None
        self.current_track_metadata = None
        self.duration = 0
        self.is_playing_flag = False
        self._eof_reached = False
        self.volume = 1.0
        self.speed = 1.0
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
        self._position = 0

        # Initialize additional properties
        self.equalizer_settings = []  # Format: [(freq, gain), ...]

        # Signal callbacks (GObject-style emulation)
        self.position_callback = None
        self.state_callback = None
        self.error_callback = None
        self.duration_callback = None
        self.eos_callback = None

        # MPV instance
        self.mpv_instance = None

        # Position update timer
        self.position_timer_id = None

        # Track selection pending
        self.pending_track_index = None

        # Seek throttling to prevent overwhelming MPV
        self.last_seek_time = 0
        self.seek_throttle_ms = 50  # Minimum time between seeks in milliseconds
        self.pending_seek_position = None
        self.seek_timer_id = None
        self.is_seeking = False

        self._sources = SourceGroup()
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
        self._create_player()

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

    def _log_handler(self, level, component, message):
        # Native decoder details are diagnostics, not untranslated GUI errors.
        if level == "error":
            logger.debug("mpv %s: %s", component, message.rstrip())

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

    def _execute_pending_seek(self):
        self.seek_timer_id = None
        position, self.pending_seek_position = self.pending_seek_position, None
        if position is not None and not self._disposed:
            self._do_seek(position)
        return False

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
    
    def _complete_seek(self, position, restore_playing):
        if self._do_seek(position) and restore_playing:
            self.play()
        return False

    def _retry_seek_after_reload(self, position, restore_playing):
        return self._complete_seek(position, restore_playing)

    def set_volume(self, volume):
        """Use the export's linear gain at the same position in the DSP chain."""
        self.volume = finite_number(volume, "Volume", 0, 10)
        self._rebuild_audio_filters()

    def set_playback_speed(self, speed):
        self.speed = finite_number(speed, "Playback speed", 0.1, 5)
        if not self._disposed and self.mpv_instance is not None:
            self.mpv_instance.speed = 1 if self.bypass_processing else self.speed

    def set_pitch_correction(self, enabled):
        self.pitch_correction = bool(enabled)
        if self.mpv_instance is not None:
            self.mpv_instance["audio-pitch-correction"] = self.pitch_correction
            self._rebuild_audio_filters()

    def _rebuild_audio_filters(self):
        if self._disposed or self.mpv_instance is None:
            return
        try:
            chain = []
            if not self.bypass_processing:
                effects = build_audio_filters(self._preview_settings(), self.gtcrn_ladspa_path)
                if effects:
                    chain.append("lavfi=[" + ",".join(effects) + "]")
                if self.pitch_correction:
                    # mpv's default scaletempo2 mutes speeds below 0.25x.
                    chain.append("scaletempo2=min-speed=0.1:max-speed=5")
                if self.normalize_enabled:
                    chain.append("lavfi=[loudnorm=I=-16:LRA=11:TP=-1.5]")
            value = ",".join(chain)
            if value != self._last_filter_chain:
                self.mpv_instance["af"] = value
                self._last_filter_chain = value
            # Export gain is in the DSP chain, before speed and normalization.
            self.mpv_instance.volume = 100
            self.mpv_instance.speed = 1 if self.bypass_processing else self.speed
        except Exception as error:
            logger.warning("Audio preview processing failed: %s", error)
            self._emit("error", str(error))
    
    def set_equalizer_bands(self, eq_bands):
        """
        Set equalizer bands for the audio playback - updates in real-time.

        Args:
            eq_bands: List of tuples (frequency, gain in dB)
                      e.g., [(60, -3), (230, 2), (910, -1), ...]
        """
        logger.debug(f"Setting equalizer bands: {eq_bands}")
        self.equalizer_settings = eq_bands
        self._rebuild_audio_filters()

    def set_noise_reduction(self, enabled):
        if enabled and not self.gtcrn_ladspa_path:
            self.noise_reduction = False
            self._emit("error", _("Neural noise reduction is unavailable. Install the GTCRN plugin and models."))
            return False
        self.noise_reduction = bool(enabled)
        self._rebuild_audio_filters()
        return True

    def set_noise_strength(self, strength):
        """Set noise reduction strength (0.0 to 1.0) and rebuild filters."""
        self.noise_strength = max(0.0, min(1.0, strength))
        logger.debug(f"Setting noise reduction strength to: {self.noise_strength}")
        if self.noise_reduction and self.gtcrn_ladspa_path:
            self._rebuild_audio_filters()

    def set_noise_model(self, model):
        """Set GTCRN model (0=DNS3, 1=VCTK)."""
        self.noise_model = model
        logger.debug(f"Setting noise model to: {model}")
        if self.noise_reduction and self.gtcrn_ladspa_path:
            self._rebuild_audio_filters()

    def set_noise_advanced(self, speech_strength=1.0, lookahead=0, voice_enhance=0.0, model_blend=False):
        """Set GTCRN advanced controls."""
        self.noise_speech_strength = speech_strength
        self.noise_lookahead = lookahead
        self.noise_voice_enhance = voice_enhance
        self.noise_model_blend = model_blend
        logger.debug(f"Noise advanced: speech={speech_strength} look={lookahead} enhance={voice_enhance} blend={model_blend}")
        if self.noise_reduction and self.gtcrn_ladspa_path:
            self._rebuild_audio_filters()

    def set_hpf_enabled(self, enabled):
        """Enable or disable high-pass filter."""
        self.hpf_enabled = enabled
        self._rebuild_audio_filters()

    def set_hpf_frequency(self, freq):
        """Set HPF cutoff frequency."""
        self.hpf_frequency = int(freq)
        if self.hpf_enabled:
            self._rebuild_audio_filters()

    def set_transient_enabled(self, enabled):
        """Enable or disable transient suppressor."""
        self.transient_enabled = enabled
        self._rebuild_audio_filters()

    def set_transient_attack(self, attack):
        """Set transient attack."""
        self.transient_attack = attack
        if self.transient_enabled:
            self._rebuild_audio_filters()

    def set_gate_enabled(self, enabled):
        """Enable or disable noise gate."""
        self.gate_enabled = enabled
        logger.debug(f"Setting noise gate to: {enabled}")
        self._rebuild_audio_filters()

    def set_gate_intensity(self, intensity):
        """Set noise gate intensity (0.0-1.0) using sqrt curve."""
        self.gate_intensity = max(0.0, min(1.0, intensity))
        logger.debug(f"Setting gate intensity to: {self.gate_intensity}")
        if self.gate_enabled:
            self._rebuild_audio_filters()

    def set_compressor_enabled(self, enabled):
        """Enable or disable compressor."""
        self.compressor_enabled = enabled
        logger.debug(f"Setting compressor to: {enabled}")
        self._rebuild_audio_filters()

    def set_compressor_intensity(self, intensity):
        """Set compressor intensity (0.0-1.0)."""
        self.compressor_intensity = max(0.0, min(1.0, intensity))
        logger.debug(f"Setting compressor intensity to: {self.compressor_intensity}")
        if self.compressor_enabled:
            self._rebuild_audio_filters()

    def is_playing(self):
        """Check if audio is currently playing."""
        return self.is_playing_flag

    def connect(self, signal_name, callback):
        """Connect signal handler."""
        if signal_name == "position-updated":
            self.position_callback = callback
        elif signal_name == "state-changed":
            self.state_callback = callback
        elif signal_name == "error":
            self.error_callback = callback
        elif signal_name == "duration-changed":
            self.duration_callback = callback
        elif signal_name == "eos":
            self.eos_callback = callback

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

    def __del__(self):
        # Application shutdown explicitly calls cleanup on the main thread.
        # Never join an mpv event thread from an arbitrary finalizer thread.
        pass

    def _post_event(self, callback, *args):
        generation = self._generation
        def deliver():
            if not self._disposed and generation == self._generation:
                callback(*args)
            return False
        self._events.idle(deliver)

    def _emit(self, name, *args):
        if self._disposed:
            return
        callback = getattr(self, name + "_callback", None)
        if callback is not None:
            callback(self, *args)

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

    def _file_ended(self, reason, identity):
        if identity is not None and self._expected_entry is not None and identity != self._expected_entry:
            return
        if reason in (0, "eof"):
            self._reached_end()
        elif reason in (4, "error"):
            self.pause()
            self._emit("error", _("Audio playback failed. Check the file and audio output device."))

    def _reached_end(self):
        if self._eof_reached or not self._loaded:
            return
        self._eof_reached = True
        self.is_playing_flag = False
        self._stop_position_timer()
        self._emit("state", False)
        self._emit("eos")

    def _stop_position_timer(self):
        self._sources.remove(self.position_timer_id)
        self.position_timer_id = None

    def _preview_settings(self):
        frequencies = (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
        equalizer = dict(self.equalizer_settings)
        return dict(volume=self.volume, speed=1.0,
                    eq_enabled=bool(equalizer), eq_bands=",".join(str(equalizer.get(f, 0)) for f in frequencies),
                    noise_reduction=self.noise_reduction, noise_strength=self.noise_strength,
                    noise_model=self.noise_model, noise_speech_strength=self.noise_speech_strength,
                    noise_lookahead=self.noise_lookahead, noise_voice_enhance=self.noise_voice_enhance,
                    noise_model_blend=self.noise_model_blend,
                    hpf_enabled=self.hpf_enabled, hpf_frequency=self.hpf_frequency,
                    transient_enabled=self.transient_enabled, transient_attack=self.transient_attack,
                    gate_enabled=self.gate_enabled, gate_intensity=self.gate_intensity,
                    compressor_enabled=self.compressor_enabled, compressor_intensity=self.compressor_intensity)

    def set_normalize_enabled(self, enabled):
        self.normalize_enabled = bool(enabled)
        self._rebuild_audio_filters()

    def set_bypass_processing(self, enabled):
        """Preview copy mode without destroying saved effect settings."""
        self.bypass_processing = bool(enabled)
        self._rebuild_audio_filters()
