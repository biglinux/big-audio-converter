"""
Audio player module using MPV for playback functionality.
"""

import gettext
import logging
import math
import os
import time
from pathlib import Path

import mpv
from gi.repository import GLib

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Audio player using MPV for robust audio playback functionality.
    """

    def __init__(self, gtcrn_ladspa_path=None):
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

        self._create_player()

    def _create_player(self):
        """Create the MPV player instance."""
        try:
            # Create MPV instance with audio-only configuration
            self.mpv_instance = mpv.MPV(
                # Audio output
                vo="null",  # No video output
                # Audio options
                audio_display="no",  # Don't show audio visualization
                # Performance
                cache="yes",
                demuxer_max_bytes="50M",
                # Log level
                log_handler=self._log_handler,
                loglevel="info",
            )

            # Set up event handlers
            @self.mpv_instance.event_callback('end-file')
            def on_end_file(event):
                try:
                    # python-mpv >= 1.0: event is MpvEvent object with .data attribute
                    if hasattr(event, "data") and hasattr(event.data, "reason"):
                        reason = event.data.reason
                    elif hasattr(event, "event") and isinstance(event.event, dict):
                        reason = event.event.get("reason", -1)
                    elif isinstance(event, dict):
                        reason = event.get("event", {}).get("reason", -1)
                    else:
                        reason = -1

                    if reason == 0:  # EOF (not error or aborted)
                        logger.info("Playback finished (EOF)")
                        self.is_playing_flag = False
                        self._position = 0
                        self._eof_reached = True

                        if self.state_callback:
                            GLib.idle_add(self.state_callback, self, False)

                        if self.eos_callback:
                            GLib.idle_add(self.eos_callback, self)
                except Exception as e:
                    logger.error(f"Error handling end-file event: {e}")

            @self.mpv_instance.event_callback('file-loaded')
            def on_file_loaded(event):
                # File loaded successfully, query duration
                try:
                    duration = self.mpv_instance.duration
                    if duration and duration > 0:
                        self.duration = duration
                        logger.info(f"Duration: {self.duration:.3f} seconds")
                        
                        if self.duration_callback:
                            GLib.idle_add(self.duration_callback, self, self.duration)
                        
                        if self.position_callback:
                            GLib.idle_add(self.position_callback, self, 0, self.duration)

                    # Ensure MPV is actually unpaused if we expect playback.
                    # play() may have set pause=False before the file finished
                    # loading; re-assert it now that the file is ready.
                    if self.is_playing_flag:
                        self.mpv_instance.pause = False
                        logger.info(
                            "on_file_loaded: re-asserted pause=False for pending playback"
                        )
                        if self.state_callback:
                            GLib.idle_add(self.state_callback, self, True)
                except Exception as e:
                    logger.error(f"Error in on_file_loaded: {e}")

            logger.info("MPV player created successfully")

        except Exception as e:
            logger.error(f"Error creating MPV player: {e}")
            if self.error_callback:
                self.error_callback(f"Failed to initialize MPV: {str(e)}")

    def _log_handler(self, loglevel, component, message):
        """Handle MPV log messages."""
        if loglevel == "error":
            logger.error(f"MPV [{component}]: {message}")
            if self.error_callback and "Failed" in message:
                GLib.idle_add(self.error_callback, f"MPV error: {message}")
        elif loglevel == "warn":
            logger.warning(f"MPV [{component}]: {message}")
        elif loglevel == "info":
            logger.info(f"MPV [{component}]: {message}")
        else:
            logger.debug(f"MPV [{component}]: {message}")

    def _position_update_callback(self):
        """Timer callback for position updates."""
        if not self.is_playing_flag or not self.mpv_instance:
            return True  # Keep timer running

        try:
            # Query current position
            position = self.mpv_instance.time_pos
            if position is not None:
                self._position = position

                # Emit position update
                if self.position_callback:
                    self.position_callback(self, self._position, self.duration)
        except Exception as e:
            logger.error(f"Position update callback error: {e}", exc_info=True)

        return True  # Continue timer

    def load(self, file_path, track_metadata=None):
        """Load an audio file or specific track from a video.

        Args:
            file_path: Path to the file (may be virtual path for tracks)
            track_metadata: Optional dict with track info for video files
        """
        # Extract actual file path if this is a track
        actual_file_path = file_path
        self.current_track_metadata = None
        self.pending_track_index = None

        # Check if this is a virtual track path (contains :: and file doesn't exist as-is)
        if "::" in file_path and not os.path.exists(file_path):
            # This is a virtual track path - need metadata
            if track_metadata and file_path in track_metadata:
                self.current_track_metadata = track_metadata[file_path]
                actual_file_path = self.current_track_metadata["source_video"]
                self.pending_track_index = self.current_track_metadata.get(
                    "track_index"
                )
                logger.info(
                    f"Loading track {self.pending_track_index} from {actual_file_path}"
                )
            else:
                logger.error(f"No track metadata found for {file_path}")
                if self.error_callback:
                    self.error_callback(f"Track metadata not found for {file_path}")
                return False
        elif "::" in file_path and os.path.exists(file_path):
            # File exists with :: in name (e.g., extracted/converted track)
            logger.info(f"Loading extracted/converted file: {file_path}")
            actual_file_path = file_path

        if not os.path.exists(actual_file_path):
            if self.error_callback:
                self.error_callback(f"File not found: {actual_file_path}")
            return False

        # Stop any current playback (skip if already stopped to avoid redundant callbacks)
        if self.is_playing_flag:
            logger.info("load(): stopping current playback before loading new file")
            self.stop()
        else:
            logger.info("load(): skipping stop (is_playing_flag=False)")

        # Store the file paths
        self.current_file = file_path
        self.current_actual_file = actual_file_path
        self._position = 0
        self._eof_reached = False

        try:
            # Pause before loading to prevent auto-play;
            # play() will set pause=False when called explicitly
            self.mpv_instance.pause = True

            # Load file in MPV
            self.mpv_instance.loadfile(actual_file_path)
            
            # If specific track index requested, select it
            if self.pending_track_index is not None:
                try:
                    self.mpv_instance.aid = self.pending_track_index
                    logger.info(f"Selected audio track: {self.pending_track_index}")
                except Exception as e:
                    logger.warning(f"Failed to select audio track {self.pending_track_index}: {e}")

            # MPV loads asynchronously, duration will be available via file-loaded event
            logger.info(f"Audio file loading: {actual_file_path}")

            return True
            
        except Exception as e:
            logger.error(f"Failed to load audio file: {e}")
            if self.error_callback:
                self.error_callback(f"Failed to load audio file: {str(e)}")
            return False

    def play(self):
        """Start or resume playback."""
        if not self.current_file or self.is_playing_flag:
            logger.debug(
                f"play() skipped: current_file={self.current_file}, is_playing_flag={self.is_playing_flag}"
            )
            return False

        logger.info(
            f"Starting playback: file={self.current_file}, timer_id={self.position_timer_id}"
        )

        try:
            # Set MPV to play
            self.mpv_instance.pause = False
            
            # Update state
            self.is_playing_flag = True
            if self.state_callback:
                GLib.idle_add(self.state_callback, self, True)

            # Start position update timer if not already running
            if self.position_timer_id is None:
                self.position_timer_id = GLib.timeout_add(
                    100, self._position_update_callback
                )

            return True
            
        except Exception as e:
            logger.error(f"Failed to start playback: {e}")
            if self.error_callback:
                self.error_callback(f"Failed to start playback: {str(e)}")
            return False

    def pause(self):
        """Pause playback, maintaining current position."""
        logger.debug("Pausing playback")

        try:
            # Set MPV to pause
            self.mpv_instance.pause = True

            # Update state
            self.is_playing_flag = False
            if self.state_callback:
                GLib.idle_add(self.state_callback, self, False)

            return True
            
        except Exception as e:
            logger.error(f"Failed to pause: {e}")
            return False

    def stop(self):
        """Stop playback."""
        logger.info(
            f"Stopping playback: is_playing={self.is_playing_flag}, file={self.current_file}"
        )

        try:
            # Stop MPV
            self.mpv_instance.command("stop")

            # Update state
            self.is_playing_flag = False
            if self.state_callback:
                GLib.idle_add(self.state_callback, self, False)

            # Reset position
            self._position = 0
            if self.position_callback:
                self.position_callback(self, 0, self.duration)

            return True
            
        except Exception as e:
            logger.error(f"Failed to stop: {e}")
            return False

    def seek(self, position):
        """Seek to a specific position in seconds with throttling for rapid seeks."""
        # Clamp position to valid range immediately
        if self.duration > 0:
            max_position = max(0, self.duration - 0.1)
            position = max(0, min(position, max_position))
        else:
            position = max(0, position)

        # Check if we're being called too rapidly
        current_time = time.time() * 1000  # milliseconds
        time_since_last_seek = current_time - self.last_seek_time

        if time_since_last_seek < self.seek_throttle_ms:
            # Too soon - schedule a delayed seek instead
            self.pending_seek_position = position

            # Cancel existing timer if any
            if self.seek_timer_id is not None:
                GLib.source_remove(self.seek_timer_id)

            # Schedule seek for later
            delay_ms = int(self.seek_throttle_ms - time_since_last_seek)
            self.seek_timer_id = GLib.timeout_add(delay_ms, self._execute_pending_seek)
            logger.debug(f"Throttling seek to {position:.3f}s, delayed by {delay_ms}ms")
            return True

        # Execute seek immediately
        return self._do_seek(position)

    def _execute_pending_seek(self):
        """Execute a pending throttled seek."""
        if self.pending_seek_position is not None:
            position = self.pending_seek_position
            self.pending_seek_position = None
            self.seek_timer_id = None
            self._do_seek(position)
        return False  # Don't repeat timer

    def _do_seek(self, position):
        """Internal method to perform actual seek operation."""
        logger.debug(f"Executing seek to position={position:.6f}s")

        # Update last seek time
        self.last_seek_time = time.time() * 1000

        # If EOF was reached, reload the file first before seeking
        if self._eof_reached and self.current_actual_file:
            logger.info(
                f"Reloading file after EOF before seek: {self.current_actual_file}"
            )
            self._eof_reached = False
            try:
                self.mpv_instance.loadfile(self.current_actual_file)
                if self.pending_track_index is not None:
                    self.mpv_instance.aid = self.pending_track_index
                self.mpv_instance.pause = True
                # Delay seek to allow file loading
                GLib.timeout_add(200, self._complete_seek, position, False)
                return True
            except Exception as e:
                logger.error(f"Failed to reload file after EOF: {e}")
                return False

        # Remember if we were playing
        was_playing = self.is_playing_flag

        try:
            # Mark that we're seeking
            self.is_seeking = True

            # For smoother segment transitions, pause briefly before seeking if playing
            # This allows the audio buffer to drain, preventing stuttering/glitches
            if was_playing:
                self.mpv_instance.pause = True
                # Small delay (20ms) to let audio buffer drain
                GLib.timeout_add(20, lambda: self._complete_seek(position, True))
            else:
                self._complete_seek(position, False)
            
            return True

        except Exception as e:
            logger.error(f"Exception during seek: {e}")
            return False
        finally:
            self.is_seeking = False
    
    def _complete_seek(self, position, restore_playing):
        """Complete the seek operation after audio buffer has been cleared."""
        try:
            # Perform seek with exact precision for smooth segment transitions
            self.mpv_instance.seek(position, reference='absolute', precision='exact')
            
            self._position = position
            logger.debug(f"Seek completed to {position:.3f}s")
            
            # Restore playing state if needed
            if restore_playing:
                self.mpv_instance.pause = False
                
        except Exception as e:
            logger.error(f"Exception completing seek: {e}")
            # After EOF, MPV unloads the file. Reload and retry seek once.
            if self.current_actual_file:
                try:
                    logger.info(
                        f"Reloading file after seek failure: {self.current_actual_file}"
                    )
                    self.mpv_instance.loadfile(self.current_actual_file)
                    if self.pending_track_index is not None:
                        self.mpv_instance.aid = self.pending_track_index
                    self.mpv_instance.pause = True
                    # Retry seek after file-loaded event via a short delay
                    GLib.timeout_add(
                        200, self._retry_seek_after_reload, position, restore_playing
                    )
                except Exception as reload_err:
                    logger.error(f"Failed to reload file for seek retry: {reload_err}")
        
        return False  # Don't repeat timer

    def _retry_seek_after_reload(self, position, restore_playing):
        """Retry a seek after reloading the file (post-EOF recovery)."""
        try:
            self.mpv_instance.seek(position, reference="absolute", precision="exact")
            self._position = position
            if restore_playing:
                self.mpv_instance.pause = False
                self.is_playing_flag = True
                if self.state_callback:
                    GLib.idle_add(self.state_callback, self, True)
            logger.info(f"Seek retry succeeded at {position:.3f}s")
        except Exception as e:
            logger.error(f"Seek retry also failed: {e}")
        return False

    def set_volume(self, volume):
        """Set playback volume (0.0 to 5.0) - updates in real-time."""
        volume = max(0.0, min(volume, 5.0))
        logger.debug(f"Setting volume to: {volume}")

        self.volume = volume
        try:
            # MPV volume is 0-100, but we support up to 5.0 (500%)
            self.mpv_instance.volume = volume * 100
        except Exception as e:
            logger.error(f"Failed to set volume: {e}")

    def set_playback_speed(self, speed):
        """Set playback speed (0.5 to 5.0) - updates in real-time."""
        speed = max(0.5, min(speed, 5.0))
        logger.debug(f"Setting playback speed to: {speed}")

        self.speed = speed
        try:
            self.mpv_instance.speed = speed
        except Exception as e:
            logger.error(f"Failed to set speed: {e}")

    def set_pitch_correction(self, enabled):
        """Enable or disable pitch correction when changing speed."""
        logger.debug(f"Setting pitch correction to: {enabled}")

        self.pitch_correction = enabled
        try:
            # MPV audio-pitch-correction: yes (preserve pitch) or no (change pitch with speed)
            self.mpv_instance['audio-pitch-correction'] = 'yes' if enabled else 'no'
        except Exception as e:
            logger.error(f"Failed to set pitch correction: {e}")

    def _rebuild_audio_filters(self):
        """Rebuild the complete audio filter chain from current settings.

        Order: HPF → Transient → Compressor → GTCRN NR → Gate → EQ
        """
        filters = []

        # 1. High-pass filter
        if self.hpf_enabled:
            filters.append(f"highpass=f={self.hpf_frequency}:poles=2")

        # 2. Transient suppressor
        if self.transient_enabled and self.gtcrn_ladspa_path:
            ladspa_dir = str(Path(self.gtcrn_ladspa_path).parent)
            filters.append(
                f"ladspa=file={ladspa_dir}/transient_split.so:plugin=transient:controls=c0={self.transient_attack}"
            )

        # 3. Compressor (before NR to even out dynamics)
        if self.compressor_enabled:
            ci = self.compressor_intensity
            threshold_db = -20.0 - ci * 20.0
            ratio = 3.0 + ci * 7.0
            makeup_db = 6.0 + ci * 12.0
            knee_db = 12.0 + ci * 4.0
            threshold_lin = 10 ** (threshold_db / 20.0)
            makeup_lin = 10 ** (makeup_db / 20.0)
            knee_lin = 10 ** (knee_db / 20.0)  # FFmpeg acompressor knee range: 1-8
            filters.append(
                f"acompressor=threshold={threshold_lin:.6f}:ratio={ratio:.1f}:attack=150:release=800"
                f":makeup={makeup_lin:.4f}:knee={knee_lin:.4f}:detection=rms"
            )

        # 4. GTCRN LADSPA noise reduction
        if self.noise_reduction and self.gtcrn_ladspa_path:
            model_blend_val = 1 if self.noise_model_blend else 0
            filters.append(
                f"ladspa=file={self.gtcrn_ladspa_path}:plugin=gtcrn_mono:controls="
                f"c0=1|c1={self.noise_strength}|c2={self.noise_model}|c3={self.noise_speech_strength}"
                f"|c4={self.noise_lookahead}|c5={self.noise_voice_enhance}|c6={model_blend_val}"
            )

        # 5. Noise gate (intensity-based)
        if self.gate_enabled:
            threshold_db = -50.0 + math.sqrt(self.gate_intensity) * 35.0
            range_db = -40.0 - math.sqrt(self.gate_intensity) * 50.0
            threshold_lin = 10 ** (threshold_db / 20.0)
            range_lin = 10 ** (range_db / 20.0)
            filters.append(
                f"agate=threshold={threshold_lin:.6f}:range={range_lin:.6f}:attack=10:release=10:detection=rms"
            )

        # 6. Equalizer
        if self.equalizer_settings:
            for freq, gain in self.equalizer_settings:
                filters.append(f"equalizer=f={freq}:width_type=o:w=1.5:g={gain}")

        # Apply the filter chain wrapped in lavfi for MPV
        # Always clear first to force LADSPA plugin re-instantiation
        try:
            try:
                del self.mpv_instance['af']
            except Exception:
                self.mpv_instance["af"] = ""

            if filters:
                graph = ",".join(filters)
                filter_string = f"lavfi=[{graph}]"
                self.mpv_instance['af'] = filter_string
                logger.debug(f"Audio filters applied: {filter_string}")
            else:
                logger.debug("All audio filters cleared")
        except Exception as e:
            logger.error(f"Failed to apply audio filters: {e}")
    
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
        """
        Enable or disable noise reduction during playback using GTCRN LADSPA plugin.

        This uses the ffmpeg ladspa audio filter via MPV for real-time noise reduction.
        The GTCRN plugin processes audio using a trained neural network model.
        """
        logger.debug(f"Setting noise reduction to: {enabled}")

        self.noise_reduction = enabled

        if not self.gtcrn_ladspa_path:
            if enabled:
                logger.warning(
                    "Noise reduction requested but GTCRN LADSPA plugin not available"
                )
            return

        try:
            # Rebuild the complete audio filter chain
            self._rebuild_audio_filters()

            if enabled:
                logger.info("GTCRN LADSPA noise reduction enabled")

        except Exception as e:
            logger.error(f"Failed to set noise reduction: {e}")
            if enabled:
                logger.warning("GTCRN LADSPA filter may not be available")

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
        """Cleanup resources."""
        # Stop position timer
        if self.position_timer_id:
            GLib.source_remove(self.position_timer_id)
            self.position_timer_id = None

        # Stop seek timer
        if self.seek_timer_id:
            GLib.source_remove(self.seek_timer_id)
            self.seek_timer_id = None

        # Stop MPV
        if self.mpv_instance:
            try:
                self.mpv_instance.command("stop")
            except Exception:
                pass

    def __del__(self):
        """Destructor to ensure cleanup."""
        self.cleanup()
