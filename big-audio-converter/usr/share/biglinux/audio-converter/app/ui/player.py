"""
Audio player module for handling audio playback.
"""

import gi
import os
import logging
from pathlib import Path

gi.require_version("Gst", "1.0")
from gi.repository import Gst, GObject, GLib

logger = logging.getLogger(__name__)


class AudioPlayer(GObject.Object):
    """
    Audio player using GStreamer for playback functionality.
    """

    __gsignals__ = {
        "position-updated": (GObject.SignalFlags.RUN_LAST, None, (float, float)),
        "state-changed": (GObject.SignalFlags.RUN_LAST, None, (bool,)),
        "duration-changed": (GObject.SignalFlags.RUN_LAST, None, (float,)),
        "error": (GObject.SignalFlags.RUN_LAST, None, (str,)),
    }

    def __init__(self):
        GObject.Object.__init__(self)

        # Initialize GStreamer if not already done
        if not Gst.is_initialized():
            Gst.init(None)

        # Create playbin element for audio playback
        self.player = Gst.ElementFactory.make("playbin", "audio-player")

        # Add audio sink
        audio_sink = Gst.ElementFactory.make("autoaudiosink", "audio-sink")
        if audio_sink:
            self.player.set_property("audio-sink", audio_sink)

        # Initialize playback properties
        self.duration = 0
        self.is_playing_flag = False
        self._update_position_timer = None

        # Connect bus for message handling
        bus = self.player.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self._on_message)

    def load(self, file_path):
        """Load an audio file."""
        if not os.path.exists(file_path):
            self.emit("error", f"File not found: {file_path}")
            return False

        # Convert to URI format
        uri = Path(file_path).as_uri()
        logger.info(f"Loading audio file: {uri}")

        # Set the URI property
        self.player.set_property("uri", uri)

        # Update state
        self.is_playing_flag = False
        self.emit("state-changed", False)

        # Query duration after a short delay to ensure file is loaded
        GLib.timeout_add(300, self._query_duration)

        return True

    def play(self):
        """Start or resume playback."""
        logger.debug("Starting playback")
        self.player.set_state(Gst.State.PLAYING)
        self.is_playing_flag = True
        self.emit("state-changed", True)

        # Start position update timer if not already running
        if not self._update_position_timer:
            self._update_position_timer = GLib.timeout_add(100, self._update_position)

    def stop(self):
        """Stop playback."""
        logger.debug("Stopping playback")
        self.player.set_state(Gst.State.NULL)
        self.is_playing_flag = False

        # Cancel position update timer
        if self._update_position_timer:
            GLib.source_remove(self._update_position_timer)
            self._update_position_timer = None

        self.emit("position-updated", 0, self.duration)
        self.emit("state-changed", False)

    def seek(self, position):
        """Seek to a specific position in seconds."""
        if self.duration > 0:
            position_ns = int(position * Gst.SECOND)
            logger.debug(f"Seeking to position: {position} seconds")

            # Perform seek
            self.player.seek_simple(
                Gst.Format.TIME,
                Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
                position_ns,
            )

            # Update UI immediately for better responsiveness
            self.emit("position-updated", position, self.duration)

    def set_volume(self, volume):
        """Set playback volume (0.0 to 1.0)."""
        self.player.set_property("volume", max(0.0, min(volume, 1.0)))

    def set_playback_speed(self, speed):
        """Set playback speed (0.5 to 2.0)."""
        speed = max(0.5, min(speed, 2.0))

        # Get current position
        success, pos = self.player.query_position(Gst.Format.TIME)
        position = pos / Gst.SECOND if success else 0

        # Set speed using seek with rate
        event = Gst.Event.new_seek(
            speed,
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE,
            Gst.SeekType.SET,
            int(position * Gst.SECOND),
            Gst.SeekType.NONE,
            0,
        )
        logger.debug(f"Setting playback speed to: {speed}")
        self.player.send_event(event)

    def is_playing(self):
        """Check if audio is currently playing."""
        return self.is_playing_flag

    def _query_duration(self):
        """Query and update the duration of the loaded audio."""
        success, duration = self.player.query_duration(Gst.Format.TIME)
        if success:
            duration_sec = duration / Gst.SECOND
            if self.duration != duration_sec:
                self.duration = duration_sec
                logger.debug(f"Media duration: {duration_sec} seconds")
                self.emit("duration-changed", duration_sec)
        return True  # Continue calling this function

    def _update_position(self):
        """Update the current playback position."""
        if self.is_playing_flag:
            success, pos = self.player.query_position(Gst.Format.TIME)
            if success:
                position = pos / Gst.SECOND
                self.emit("position-updated", position, self.duration)

        # Continue timer as long as playing
        return self.is_playing_flag

    def _on_message(self, bus, message):
        """Handle GStreamer bus messages."""
        message_type = message.type

        if message_type == Gst.MessageType.EOS:  # End of stream
            logger.debug("End of stream reached")
            self.stop()

        elif message_type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            logger.error(f"GStreamer error: {err.message}")
            self.emit("error", f"Playback error: {err.message}")
            self.stop()

        elif message_type == Gst.MessageType.STATE_CHANGED:
            if message.src == self.player:
                new_state, pending_state = message.parse_state_changed()
                if new_state == Gst.State.PLAYING:
                    # We've started playing, ensure position updates are working
                    if not self._update_position_timer:
                        self._update_position_timer = GLib.timeout_add(
                            100, self._update_position
                        )
                    # Also query duration again as it might be available now
                    self._query_duration()
