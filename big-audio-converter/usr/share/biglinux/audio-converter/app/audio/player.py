"""
Audio player module using GStreamer for playback functionality.
"""

import os
import logging
import gettext

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Audio player using GStreamer for robust audio playback functionality.
    """

    def __init__(self, arnndn_model_path=None):
        """Initialize the audio player with GStreamer."""
        # Initialize GStreamer
        Gst.init(None)

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

        # GObject-style signal emulation
        self.position_callback = None
        self.state_callback = None
        self.error_callback = None
        self.duration_callback = None
        self.eos_callback = None

        # Create GStreamer pipeline
        self.pipeline = None
        self.source = None
        self.decodebin = None
        self.audioconvert = None
        self.audiorate = None
        self.volume_element = None
        self.scaletempo = None
        self.equalizer = None
        self.arnndn_element = None
        self.sink = None
        self.bus = None

        # Position update timer
        self.position_timer_id = None

        # Track selection pending
        self.pending_track_index = None

        # Seek throttling to prevent overwhelming GStreamer
        self.last_seek_time = 0
        self.seek_throttle_ms = 50  # Minimum time between seeks in milliseconds
        self.pending_seek_position = None
        self.seek_timer_id = None
        self.is_seeking = False

        self._create_pipeline()

    def _create_pipeline(self):
        """Create the GStreamer pipeline and elements."""
        try:
            # Create pipeline
            self.pipeline = Gst.Pipeline.new("audio-player")

            # Create elements
            self.source = Gst.ElementFactory.make("filesrc", "source")
            self.decodebin = Gst.ElementFactory.make("decodebin", "decoder")
            self.audioconvert = Gst.ElementFactory.make("audioconvert", "convert")
            self.audiorate = Gst.ElementFactory.make("audioresample", "resample")
            self.volume_element = Gst.ElementFactory.make("volume", "volume")
            self.scaletempo = Gst.ElementFactory.make("scaletempo", "scaletempo")
            self.equalizer = Gst.ElementFactory.make("equalizer-10bands", "equalizer")
            self.sink = Gst.ElementFactory.make("autoaudiosink", "sink")

            # Note: ARNNDN element is not available as a GStreamer plugin
            # Noise reduction only works during file conversion with FFmpeg
            self.arnndn_element = None
            if self.arnndn_model_path:
                logger.info(
                    f"ARNNDN model available for conversion: {self.arnndn_model_path} "
                    "(Note: Live noise reduction in playback is not supported)"
                )

            # Check if all elements were created
            required_elements = [
                (self.source, "filesrc"),
                (self.decodebin, "decodebin"),
                (self.audioconvert, "audioconvert"),
                (self.audiorate, "audioresample"),
                (self.volume_element, "volume"),
                (self.scaletempo, "scaletempo"),
                (self.equalizer, "equalizer-10bands"),
                (self.sink, "autoaudiosink"),
            ]

            for element, name in required_elements:
                if not element:
                    logger.error(f"Failed to create GStreamer element: {name}")
                    raise RuntimeError(f"GStreamer element {name} not available")

            # Add elements to pipeline
            self.pipeline.add(self.source)
            self.pipeline.add(self.decodebin)
            self.pipeline.add(self.audioconvert)
            self.pipeline.add(self.audiorate)
            self.pipeline.add(self.volume_element)
            self.pipeline.add(self.scaletempo)
            self.pipeline.add(self.equalizer)
            self.pipeline.add(self.sink)

            # Link static elements
            self.source.link(self.decodebin)

            # Link the audio chain (after decodebin)
            # Chain: audioconvert -> audiorate -> volume -> scaletempo -> equalizer -> sink
            self.audioconvert.link(self.audiorate)
            self.audiorate.link(self.volume_element)
            self.volume_element.link(self.scaletempo)
            self.scaletempo.link(self.equalizer)
            self.equalizer.link(self.sink)

            # Connect decodebin's pad-added signal
            self.decodebin.connect("pad-added", self._on_pad_added)

            # Set up bus message handling
            self.bus = self.pipeline.get_bus()
            self.bus.add_signal_watch()
            self.bus.connect("message", self._on_bus_message)

            # Initialize volume (scaletempo handles tempo automatically via playback rate)
            self.volume_element.set_property("volume", self.volume)

            logger.info("GStreamer pipeline created successfully")

        except Exception as e:
            logger.error(f"Error creating GStreamer pipeline: {e}")
            if self.error_callback:
                self.error_callback(f"Failed to initialize GStreamer: {str(e)}")

    def _on_pad_added(self, decodebin, pad):
        """Handle pad-added signal from decodebin.

        Only processes audio streams, completely ignoring video streams.
        This ensures we work directly with audio data, avoiding video keyframe constraints.
        """
        # Get the pad capabilities
        caps = pad.get_current_caps()
        if not caps:
            return

        structure = caps.get_structure(0)
        name = structure.get_name()

        # Only link audio pads - explicitly ignore video streams
        if name.startswith("audio/"):
            # Link to audioconvert
            sink_pad = self.audioconvert.get_static_pad("sink")
            if not sink_pad.is_linked():
                pad.link(sink_pad)
                logger.info(f"Linked audio pad: {name} (video streams ignored)")
        elif name.startswith("video/"):
            # Explicitly log that we're ignoring video streams
            logger.debug(f"Ignoring video pad: {name} (audio-only processing)")

    def _on_bus_message(self, bus, message):
        """Handle GStreamer bus messages."""
        msg_type = message.type

        if msg_type == Gst.MessageType.EOS:
            # End of stream - playback finished
            logger.info("Playback finished (EOS)")
            self.is_playing_flag = False
            self._position = 0

            if self.state_callback:
                GLib.idle_add(self.state_callback, self, False)

            if self.eos_callback:
                GLib.idle_add(self.eos_callback, self)

        elif msg_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"GStreamer error: {err.message}")
            logger.debug(f"Debug info: {debug}")

            self.is_playing_flag = False

            if self.error_callback:
                GLib.idle_add(self.error_callback, f"Playback error: {err.message}")

        elif msg_type == Gst.MessageType.STATE_CHANGED:
            if message.src == self.pipeline:
                old_state, new_state, pending_state = message.parse_state_changed()
                logger.debug(
                    f"Pipeline state changed: {old_state.value_nick} -> {new_state.value_nick}"
                )

        elif msg_type == Gst.MessageType.ASYNC_DONE:
            # Query duration after seeking or state change
            self._update_duration()

    def _update_duration(self):
        """Query and update the duration from the pipeline."""
        success, duration = self.pipeline.query_duration(Gst.Format.TIME)
        if success and duration > 0:
            old_duration = self.duration
            self.duration = duration / Gst.SECOND

            # Only log if duration changed significantly (more than 0.1s difference)
            if abs(self.duration - old_duration) > 0.1:
                logger.info(f"Duration updated: {self.duration:.3f} seconds")

            if self.duration_callback and abs(self.duration - old_duration) > 0.1:
                GLib.idle_add(self.duration_callback, self, self.duration)
        else:
            logger.debug(
                f"Duration query failed: success={success}, duration={duration if success else 'N/A'}"
            )

    def _retry_duration_query(self, attempt=0, max_attempts=10):
        """Retry duration query with timeout (for files that take time to analyze)."""
        success, duration = self.pipeline.query_duration(Gst.Format.TIME)

        if success and duration > 0:
            old_duration = self.duration
            self.duration = duration / Gst.SECOND
            logger.info(
                f"Duration query succeeded (attempt {attempt + 1}): {self.duration:.3f} seconds"
            )

            if self.duration_callback and abs(self.duration - old_duration) > 0.1:
                GLib.idle_add(self.duration_callback, self, self.duration)

            if self.position_callback:
                GLib.idle_add(self.position_callback, self, 0, self.duration)

            return False  # Stop retrying

        # Retry if we haven't exceeded max attempts
        if attempt < max_attempts:
            logger.debug(
                f"Duration query attempt {attempt + 1} failed, retrying in 100ms..."
            )
            return True  # Continue retrying
        else:
            logger.warning(f"Duration query failed after {max_attempts} attempts")
            return False  # Stop retrying

    def _position_update_callback(self):
        """Timer callback for position updates."""
        if not self.is_playing_flag:
            return True  # Keep timer running

        # Query current position
        success, position = self.pipeline.query_position(Gst.Format.TIME)
        if success:
            self._position = position / Gst.SECOND

            # Emit position update
            if self.position_callback:
                self.position_callback(self, self._position, self.duration)

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

        # Set the file location
        self.source.set_property("location", actual_file_path)

        # Set pipeline to PAUSED to preload
        self.pipeline.set_state(Gst.State.PAUSED)

        # Wait for state change and query duration
        ret = self.pipeline.get_state(Gst.CLOCK_TIME_NONE)
        if (
            ret[0] == Gst.StateChangeReturn.SUCCESS
            or ret[0] == Gst.StateChangeReturn.ASYNC
        ):
            # Try immediate duration query
            self._update_duration()

            # If duration is still 0, set up retry timer
            if self.duration == 0:
                logger.info(
                    "Duration not immediately available, setting up retry mechanism..."
                )

                # Create a closure to track retry attempts
                retry_count = [0]  # Use list to allow modification in nested function

                def retry_callback():
                    result = self._retry_duration_query(retry_count[0])
                    retry_count[0] += 1
                    return result

                GLib.timeout_add(100, retry_callback)

            logger.info(
                f"Audio file loaded: {actual_file_path}, duration: {self.duration:.3f} seconds"
            )

            if self.position_callback:
                self.position_callback(self, 0, self.duration)

            return True
        else:
            logger.error("Failed to load audio file")
            if self.error_callback:
                self.error_callback("Failed to load audio file")
            return False

    def play(self):
        """Start or resume playback."""
        if not self.current_file or self.is_playing_flag:
            return False

        logger.debug("Starting playback")

        # Set pipeline to PLAYING
        ret = self.pipeline.set_state(Gst.State.PLAYING)
        if ret == Gst.StateChangeReturn.FAILURE:
            logger.error("Failed to start playback")
            if self.error_callback:
                self.error_callback("Failed to start playback")
            return False

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

    def pause(self):
        """Pause playback, maintaining current position."""
        logger.debug("Pausing playback")

        # Set pipeline to PAUSED
        self.pipeline.set_state(Gst.State.PAUSED)

        # Update state
        self.is_playing_flag = False
        if self.state_callback:
            GLib.idle_add(self.state_callback, self, False)

        # Position is preserved for resume - no need to callback since nothing changed
        # (removing position_callback here prevents infinite recursion)

        return True

    def stop(self):
        """Stop playback."""
        logger.debug("Stopping playback")

        # Set pipeline to NULL
        self.pipeline.set_state(Gst.State.NULL)

        # Update state
        self.is_playing_flag = False
        if self.state_callback:
            GLib.idle_add(self.state_callback, self, False)

        # Reset position
        self._position = 0
        if self.position_callback:
            self.position_callback(self, 0, self.duration)

        return True

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
        """Internal method to perform actual seek operation with resilience."""
        import time

        logger.debug(
            f"Executing seek to position={position:.6f}s (duration={self.duration:.6f}s)"
        )

        # Update last seek time
        self.last_seek_time = time.time() * 1000

        # If duration is not yet known, try to query it now
        if self.duration <= 0:
            logger.debug("Duration not available, querying now...")
            self._update_duration()

        # Remember if we were playing
        was_playing = self.is_playing_flag

        try:
            # Check current pipeline state
            ret = self.pipeline.get_state(0)
            current_state = ret[1]

            # Ensure pipeline is at least in PAUSED state for seeking to work
            if current_state == Gst.State.NULL:
                logger.debug("Pipeline in NULL state, setting to PAUSED for seeking")
                self.pipeline.set_state(Gst.State.PAUSED)
                # Wait for state change with timeout
                ret = self.pipeline.get_state(2 * Gst.SECOND)  # 2 second timeout
                if ret[0] == Gst.StateChangeReturn.FAILURE:
                    logger.error("Failed to set pipeline to PAUSED state")
                    # Try to recover by stopping and reloading
                    self._recover_pipeline()
                    return False

            # Mark that we're seeking
            self.is_seeking = True

            # Perform seek with ACCURATE flag to ignore video keyframes and seek precisely on audio
            # This ensures we get exact position on the audio stream, not constrained by video keyframes
            seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE
            success = self.pipeline.seek_simple(
                Gst.Format.TIME, seek_flags, int(position * Gst.SECOND)
            )

            if success:
                self._position = position
                logger.debug(f"Seek successful to {position:.3f}s")

                # Restore playing state if we were playing
                if was_playing and not self.is_playing_flag:
                    logger.debug("Restoring playing state after seek")
                    GLib.timeout_add(10, self._restore_playing_state)

                return True
            else:
                logger.error(f"Seek failed to {position:.3f}s")
                # Try to recover from failed seek
                self._recover_from_failed_seek(position, was_playing)
                return False

        except Exception as e:
            logger.error(f"Exception during seek: {e}")
            self._recover_from_failed_seek(position, was_playing)
            return False
        finally:
            self.is_seeking = False

    def _restore_playing_state(self):
        """Restore playing state after a seek operation."""
        if not self.is_playing_flag:
            self.play()
        return False  # Don't repeat

    def _recover_from_failed_seek(self, position, was_playing):
        """Attempt to recover from a failed seek operation."""
        logger.warning(f"Attempting to recover from failed seek to {position:.3f}s")
        try:
            # Try to set pipeline back to PAUSED state
            self.pipeline.set_state(Gst.State.PAUSED)
            ret = self.pipeline.get_state(2 * Gst.SECOND)

            if (
                ret[0] == Gst.StateChangeReturn.SUCCESS
                or ret[0] == Gst.StateChangeReturn.ASYNC
            ):
                # Try seek again with less strict flags
                seek_flags = Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT
                success = self.pipeline.seek_simple(
                    Gst.Format.TIME, seek_flags, int(position * Gst.SECOND)
                )
                if success:
                    logger.info("Recovery seek successful with KEY_UNIT flag")
                    self._position = position
                    if was_playing:
                        GLib.timeout_add(10, self._restore_playing_state)
                    return

            # If still failing, try full recovery
            self._recover_pipeline()

        except Exception as e:
            logger.error(f"Exception during seek recovery: {e}")
            self._recover_pipeline()

    def _recover_pipeline(self):
        """Recover pipeline from stuck state by reloading the file."""
        logger.warning("Attempting full pipeline recovery")
        try:
            if self.current_file:
                # Save state
                file_path = self.current_file
                was_playing = self.is_playing_flag
                position = self._position

                # Stop and reload
                self.stop()
                if self.load(file_path, self.current_track_metadata):
                    # Restore position
                    GLib.timeout_add(100, lambda: self._do_seek(position))
                    if was_playing:
                        GLib.timeout_add(200, self._restore_playing_state)
                    logger.info("Pipeline recovery successful")
                else:
                    logger.error("Pipeline recovery failed - could not reload file")
        except Exception as e:
            logger.error(f"Exception during pipeline recovery: {e}")

    def set_volume(self, volume):
        """Set playback volume (0.0 to 5.0) - updates in real-time."""
        volume = max(0.0, min(volume, 5.0))
        logger.debug(f"Setting volume to: {volume}")

        self.volume = volume
        self.volume_element.set_property("volume", volume)

    def set_playback_speed(self, speed):
        """Set playback speed (0.5 to 5.0) - updates in real-time."""
        speed = max(0.5, min(speed, 5.0))
        logger.debug(f"Setting playback speed to: {speed}")

        self.speed = speed

        # Scaletempo adjusts automatically based on pipeline playback rate
        # We need to perform a seek with the new rate
        if self.is_playing_flag or self.pipeline.get_state(0)[1] != Gst.State.NULL:
            # Get current position
            success, position = self.pipeline.query_position(Gst.Format.TIME)
            if success:
                # Perform seek with new rate
                self.pipeline.seek(
                    speed,  # rate
                    Gst.Format.TIME,
                    Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
                    Gst.SeekType.SET,
                    position,  # Start position
                    Gst.SeekType.NONE,
                    -1,  # Stop position (play to end)
                )

    def set_pitch_correction(self, enabled):
        """Enable or disable pitch correction when changing speed.

        Note: Scaletempo always preserves pitch. To disable pitch correction,
        we would need to use a different element or approach.
        For now, this is a no-op as scaletempo doesn't support pitch changes.
        """
        logger.debug(
            f"Setting pitch correction to: {enabled} (scaletempo always preserves pitch)"
        )

        self.pitch_correction = enabled

        # Scaletempo always preserves pitch - we would need to swap elements
        # to implement non-pitch-corrected speed changes
        # This could be a future enhancement with dynamic pipeline modification

    def set_equalizer_bands(self, eq_bands):
        """
        Set equalizer bands for the audio playback - updates in real-time.

        Args:
            eq_bands: List of tuples (frequency, gain in dB)
                      e.g., [(60, -3), (230, 2), (910, -1), ...]
        """
        logger.debug(f"Setting equalizer bands: {eq_bands}")
        self.equalizer_settings = eq_bands

        # Map frequencies to the 10-band equalizer
        # Standard 10-band EQ frequencies: 29, 59, 119, 237, 474, 947, 1889, 3770, 7523, 15011 Hz
        standard_freqs = [29, 59, 119, 237, 474, 947, 1889, 3770, 7523, 15011]

        # Initialize all bands to 0
        band_gains = [0.0] * 10

        # Map user frequencies to closest band
        for freq, gain in eq_bands:
            # Find closest standard frequency
            closest_band = min(
                range(len(standard_freqs)), key=lambda i: abs(standard_freqs[i] - freq)
            )
            band_gains[closest_band] = gain

        # Apply to equalizer
        for band_idx, gain in enumerate(band_gains):
            self.equalizer.set_property(f"band{band_idx}", gain)

    def set_noise_reduction(self, enabled):
        """
        Enable or disable noise reduction during playback.

        NOTE: Live noise reduction is not currently supported in GStreamer playback
        because the ARNNDN plugin is not available as a native GStreamer element.

        The arnndn filter works in FFmpeg for file conversion, but cannot be applied
        in real-time during playback without significant performance impact.

        This method is kept for API compatibility but logs a warning that the feature
        is not available for live playback.
        """
        logger.warning(
            f"Noise reduction toggle to {enabled} requested, but live noise reduction "
            "is not supported in GStreamer playback. Use file conversion instead."
        )

        self.noise_reduction = enabled

        if not self.arnndn_model_path:
            if enabled:
                logger.warning(
                    "Noise reduction requested but ARNNDN model not available"
                )
            return

        # Future enhancement: Could implement using FFmpeg subprocess with named pipes
        # For now, noise reduction only works during file conversion
        logger.info(
            "Noise reduction is only available during file conversion, not live playback. "
            "The ARNNDN filter requires FFmpeg and is not available as a GStreamer plugin."
        )

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

        # Stop pipeline
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)

        # Remove bus watch
        if self.bus:
            self.bus.remove_signal_watch()

    def __del__(self):
        """Destructor to ensure cleanup."""
        self.cleanup()
