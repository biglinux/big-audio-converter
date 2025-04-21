"""
Audio player module for handling audio playback.
"""

import os
import subprocess
import threading
import time
import logging

logger = logging.getLogger(__name__)


class AudioPlayer:
    """
    Audio player using ffplay for playback functionality.
    """

    def __init__(self):
        """Initialize the audio player."""
        self.ffplay_path = self._find_ffplay()
        if not self.ffplay_path:
            logger.error("ffplay not found! Audio playback will not work.")

        # Initialize playback properties
        self.current_file = None
        self.duration = 0
        self.is_playing_flag = False
        self.volume = 1.0
        self.speed = 1.0
        self.pitch_correction = True
        self.ffplay_process = None
        self.noise_reduction = False

        # Position tracking
        self._position = 0
        self._position_update_thread = None
        self._stop_position_thread = False

        # Initialize additional properties
        self.equalizer_settings = []  # Format: [(freq, gain), ...]

        # GObject-style signal emulation
        self.position_callback = None
        self.state_callback = None
        self.error_callback = None
        self.duration_callback = None

    def _find_ffplay(self):
        """Find the ffplay executable in the PATH."""
        # Try to find ffplay using the same approach as for ffmpeg
        try:
            # Try the ffplay command
            result = subprocess.run(
                ["ffplay", "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode == 0:
                return "ffplay"
        except FileNotFoundError:
            pass

        return None

    def _get_duration(self, file_path):
        """Get the duration of an audio file in seconds using ffprobe."""
        if not self.ffplay_path:
            return 0

        # If ffplay is found, ffprobe should be in the same location
        ffprobe_path = self.ffplay_path.replace("ffplay", "ffprobe")
        if not os.path.exists(ffprobe_path):
            ffprobe_path = "ffprobe"  # Try using the command directly

        try:
            cmd = [
                ffprobe_path,
                "-i",
                file_path,
                "-show_entries",
                "format=duration",
                "-v",
                "quiet",
                "-of",
                "csv=p=0",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout:
                return float(result.stdout.strip())
        except Exception as e:
            logger.error(f"Error getting duration: {str(e)}")

        return 0

    def _position_update_loop(self):
        """Thread function to update position during playback."""
        start_time = time.time()
        while (
            not self._stop_position_thread
            and self.ffplay_process
            and self.ffplay_process.poll() is None
        ):
            # Calculate position based on elapsed time and playback speed
            elapsed = time.time() - start_time
            position = min(self._position + (elapsed * self.speed), self.duration)

            # Call position callback with self as first argument
            if self.position_callback:
                self.position_callback(self, position, self.duration)

            time.sleep(0.1)

        # If playback ended naturally, signal that we're stopped
        if not self._stop_position_thread and self.is_playing_flag:
            self.is_playing_flag = False
            if self.state_callback:
                # Pass self as the first argument
                self.state_callback(self, False)

    def load(self, file_path):
        """Load an audio file."""
        if not self.ffplay_path:
            if self.error_callback:
                self.error_callback("ffplay not found. Please install FFmpeg.")
            return False

        if not os.path.exists(file_path):
            if self.error_callback:
                self.error_callback(f"File not found: {file_path}")
            return False

        # Stop any current playback
        self.stop()

        # Store the new file
        self.current_file = file_path
        self._position = 0

        # Get duration
        self.duration = self._get_duration(file_path)
        if self.duration_callback:
            # Pass self as the first argument
            self.duration_callback(self, self.duration)

        if self.position_callback:
            # Pass self as the first argument
            self.position_callback(self, 0, self.duration)

        return True

    def play(self):
        """Start or resume playback."""
        if not self.current_file or self.is_playing_flag:
            return False

        logger.debug("Starting playback")

        # Build ffplay command
        cmd = [self.ffplay_path]

        # Make ffplay exit when finished
        cmd.extend(["-autoexit"])

        # Hide ffplay window
        cmd.extend(["-nodisp"])

        # Set volume
        cmd.extend(["-volume", str(int(self.volume * 100))])

        # Set up audio filters
        audio_filters = []

        # Add speed filter with/without pitch correction
        if self.speed != 1.0:
            if self.pitch_correction:
                # Use atempo filter for speed adjustment with pitch preservation
                audio_filters.append(f"atempo={self.speed}")
            else:
                # Use setpts filter for speed adjustment without pitch preservation
                audio_filters.append(f"asetrate=44100*{self.speed}")

        # Apply noise reduction if enabled
        if self.noise_reduction:
            # Add high-pass filter to reduce low-frequency noise
            audio_filters.append("highpass=f=200")
            # Add low-pass filter to reduce high-frequency hiss
            audio_filters.append("lowpass=f=3000")
            # Add dynamic noise suppression
            audio_filters.append("afftdn=nf=-20")

        # Add equalizer settings if any
        if self.equalizer_settings:
            eq_parts = []
            for freq, gain in self.equalizer_settings:
                # Use FFmpeg's equalizer filter for each band
                # width_type=o means octave bands
                eq_parts.append(f"equalizer=f={freq}:width_type=o:width=1:g={gain}")

            # Append all equalizer parts to the filter chain
            audio_filters.extend(eq_parts)

        # Apply all audio filters
        if audio_filters:
            cmd.extend(["-af", ",".join(audio_filters)])

        # Start from specified position
        if self._position > 0:
            cmd.extend(["-ss", str(self._position)])

        # Add input file
        cmd.extend([self.current_file])

        # Log the full command for debugging
        logger.debug(f"ffplay command: {' '.join(cmd)}")

        try:
            # Start ffplay process
            self.ffplay_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
            )

            # Update flag and notify
            self.is_playing_flag = True
            if self.state_callback:
                # Pass self as the first argument
                self.state_callback(self, True)

            # Start position tracking thread
            self._stop_position_thread = False
            self._position_update_thread = threading.Thread(
                target=self._position_update_loop, daemon=True
            )
            self._position_update_thread.start()

            return True
        except Exception as e:
            logger.error(f"Error playing audio: {str(e)}")
            if self.error_callback:
                self.error_callback(f"Playback error: {str(e)}")
            return False

    def stop(self):
        """Stop playback."""
        logger.debug("Stopping playback")

        # Stop position update thread
        self._stop_position_thread = True

        # Terminate ffplay process
        if self.ffplay_process:
            try:
                # Try to terminate gracefully
                self.ffplay_process.terminate()

                # Give it 0.5 seconds to terminate
                for _ in range(5):
                    if self.ffplay_process.poll() is not None:
                        break
                    time.sleep(0.1)

                # If still running, force kill
                if self.ffplay_process.poll() is None:
                    self.ffplay_process.kill()

            except Exception as e:
                logger.error(f"Error stopping playback: {str(e)}")

            self.ffplay_process = None

        # Update state
        self.is_playing_flag = False
        if self.state_callback:
            # Pass self as the first argument (emulating GObject signals)
            self.state_callback(self, False)

        # Reset position
        self._position = 0
        if self.position_callback:
            # Pass self as the first argument
            self.position_callback(self, 0, self.duration)

        return True

    def seek(self, position):
        """Seek to a specific position in seconds."""
        if self.duration <= 0:
            return False

        # Clamp position to valid range
        position = max(0, min(position, self.duration))
        logger.debug(f"Seeking to position: {position} seconds")

        # If playing, stop and restart at new position
        was_playing = self.is_playing_flag
        if was_playing:
            self.stop()

        # Set new position
        self._position = position

        # Report new position
        if self.position_callback:
            # Pass self as the first argument
            self.position_callback(self, position, self.duration)

        # If was playing, restart playback
        if was_playing:
            self.play()

        return True

    def set_volume(self, volume):
        """Set playback volume (0.0 to 2.0)."""
        # Ensure volume is within valid range
        volume = max(0.0, min(volume, 2.0))
        logger.debug(f"Setting volume to: {volume}")

        # Store the volume
        self.volume = volume

        # If playing, restart with new volume
        if self.is_playing_flag:
            current_position = self._position
            self.stop()
            self._position = current_position
            self.play()

    def set_playback_speed(self, speed):
        """Set playback speed (0.5 to 2.0)."""
        # Ensure speed is within valid range
        speed = max(0.5, min(speed, 2.0))
        logger.debug(f"Setting playback speed to: {speed}")

        # Store the speed
        self.speed = speed

        # If playing, restart with new speed
        if self.is_playing_flag:
            current_position = self._position
            self.stop()
            self._position = current_position
            self.play()

    def set_pitch_correction(self, enabled):
        """Enable or disable pitch correction when changing speed."""
        logger.debug(f"Setting pitch correction to: {enabled}")

        # Store the setting
        self.pitch_correction = enabled

        # If playing with non-default speed, restart to apply the change
        if self.is_playing_flag and self.speed != 1.0:
            current_position = self._position
            self.stop()
            self._position = current_position
            self.play()

    def set_equalizer_bands(self, eq_bands):
        """
        Set equalizer bands for the audio playback.

        Args:
            eq_bands: List of tuples (frequency, gain in dB)
                      e.g., [(60, -3), (230, 2), (910, -1), ...]
        """
        logger.debug(f"Setting equalizer bands: {eq_bands}")
        self.equalizer_settings = eq_bands

        # If playing, restart with new equalizer settings
        if self.is_playing_flag:
            current_position = self._position
            self.stop()
            self._position = current_position
            self.play()

    def set_noise_reduction(self, enabled):
        """Enable or disable noise reduction during playback."""
        logger.debug(f"Setting noise reduction to: {enabled}")

        # Store the setting
        self.noise_reduction = enabled

        # If playing, restart with new setting
        if self.is_playing_flag:
            current_position = self._position
            self.stop()
            self._position = current_position
            self.play()

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
