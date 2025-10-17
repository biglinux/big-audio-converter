"""
Audio player module using MPV for playback functionality.
"""

import os
import logging
import gettext
import mpv

from gi.repository import GLib

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Audio player using MPV for robust audio playback functionality.
    """

    def __init__(self, arnndn_model_path=None):
        """Initialize the audio player with MPV."""
        self.arnndn_model_path = arnndn_model_path
        if self.arnndn_model_path and not os.path.exists(self.arnndn_model_path):
            logger.warning(
                f"Provided ARNNDN model path for player does not exist: {self.arnndn_model_path}"
            )
            self.arnndn_model_path = None

        # Initialize playback properties
        self.current_file = None
        self.current_actual_file = None
        self.current_track_metadata = None
        self.duration = 0
        self.is_playing_flag = False
        self.volume = 1.0
        self.speed = 1.0
        self.pitch_correction = True
        self.noise_reduction = False
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
                if event['event']['reason'] == 0:  # EOF (not error or aborted)
                    logger.info("Playback finished (EOF)")
                    self.is_playing_flag = False
                    self._position = 0
                    
                    if self.state_callback:
                        GLib.idle_add(self.state_callback, self, False)
                    
                    if self.eos_callback:
                        GLib.idle_add(self.eos_callback, self)

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
                except:
                    pass

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
        except:
            pass

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

        # Stop any current playback
        self.stop()

        # Store the file paths
        self.current_file = file_path
        self.current_actual_file = actual_file_path
        self._position = 0

        try:
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
            return False

        logger.debug("Starting playback")

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
        logger.debug("Stopping playback")

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
        import time

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
        import time

        logger.debug(f"Executing seek to position={position:.6f}s")

        # Update last seek time
        self.last_seek_time = time.time() * 1000

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
        
        return False  # Don't repeat timer

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
        """Rebuild the complete audio filter chain from current settings."""
        filters = []
        
        # Add equalizer if configured
        if self.equalizer_settings:
            eq_filters = []
            for freq, gain in self.equalizer_settings:
                width = freq
                eq_filters.append(f"equalizer=f={freq}:t=peak:w={width}:g={gain}")
            filters.extend(eq_filters)
        
        # Add ARNNDN if enabled
        if self.noise_reduction and self.arnndn_model_path:
            filters.append(f"arnndn=model={self.arnndn_model_path}:mix=1.0")
        
        # Apply the filter chain
        try:
            if filters:
                filter_string = ",".join(filters)
                self.mpv_instance['af'] = filter_string
                logger.debug(f"Audio filters applied: {filter_string}")
            else:
                # Clear all filters
                try:
                    del self.mpv_instance['af']
                except:
                    self.mpv_instance['af'] = ""
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

        try:
            # Build MPV equalizer filter string
            # Format: f=freq:t=peak:w=width:g=gain
            if eq_bands:
                eq_filters = []
                for freq, gain in eq_bands:
                    # Use octave width for natural sounding EQ
                    width = freq
                    eq_filters.append(f"f={freq}:t=peak:w={width}:g={gain}")
                
                eq_string = ",".join([f"equalizer={f}" for f in eq_filters])
                self.mpv_instance['af'] = eq_string
            else:
                # Clear equalizer
                self.mpv_instance['af'] = ""
                
        except Exception as e:
            logger.error(f"Failed to set equalizer: {e}")

    def set_noise_reduction(self, enabled):
        """
        Enable or disable noise reduction during playback using ARNNDN filter.

        This uses the MPV ARNNDN audio filter for real-time noise reduction.
        The filter processes audio in real-time using a trained RNN model.
        """
        logger.debug(f"Setting noise reduction to: {enabled}")

        self.noise_reduction = enabled

        if not self.arnndn_model_path:
            if enabled:
                logger.warning(
                    "Noise reduction requested but ARNNDN model not available"
                )
            return

        try:
            if enabled:
                # Enable ARNNDN filter with model
                # Format: arnndn=model=<path>:mix=<0.0-1.0>
                # mix=1.0 means 100% processed audio (full noise reduction)
                arnndn_filter = f"arnndn=model={self.arnndn_model_path}:mix=1.0"
                
                # Get current audio filters if any
                current_filters = self.mpv_instance['af']
                
                # Add ARNNDN filter
                if current_filters and current_filters != "":
                    # Append to existing filters
                    self.mpv_instance['af'] = f"{current_filters},{arnndn_filter}"
                else:
                    # Set as only filter
                    self.mpv_instance['af'] = arnndn_filter
                
                logger.info(f"ARNNDN noise reduction enabled with model: {self.arnndn_model_path}")
            
            # Rebuild the complete audio filter chain
            self._rebuild_audio_filters()
                    
        except Exception as e:
            logger.error(f"Failed to set noise reduction: {e}")
            if enabled:
                logger.warning("ARNNDN filter may not be available in your MPV build")

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
            except:
                pass

    def __del__(self):
        """Destructor to ensure cleanup."""
        self.cleanup()
