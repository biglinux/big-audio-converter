# app/ui/playback_controller.py

"""
Playback Controller Mixin for MainWindow.

Extracts all playback-related methods (play, pause, seek, segment transitions,
auto-advance, file navigation) from MainWindow into a reusable mixin.

Usage:
    class MainWindow(PlaybackControllerMixin, Adw.ApplicationWindow):
        ...
"""

import logging
import threading

from gi.repository import GLib

from app.audio import waveform
from app.utils.time_formatter import format_time_short

logger = logging.getLogger(__name__)


class PlaybackControllerMixin:
    """Mixin providing all playback control logic for MainWindow."""

    # --- Playback lifecycle ---

    def on_playback_finished(self, player):
        """Handle playback completion and auto-play next file."""
        logger.info("Playback finished, checking for next track")

        auto_advance_enabled = (
            self.auto_advance_switch.get_active()
            if hasattr(self, "auto_advance_switch")
            else True
        )
        if not auto_advance_enabled:
            logger.info("Auto-advance disabled, stopping playback")
            GLib.idle_add(self.file_queue.update_playing_state, False)
            return

        current_index = self.file_queue.get_current_playing_index()
        if current_index is None:
            logger.debug("No current track index found")
            return

        files = self.file_queue.get_files()
        if not files:
            logger.debug("Queue is empty")
            return

        next_index = current_index + 1
        if next_index < len(files):
            logger.info(f"Auto-playing next file (index {next_index})")
            next_file = files[next_index]
            GLib.timeout_add(300, self._play_next_file, next_file, next_index)
        else:
            logger.info("Reached end of queue, stopping playback")
            GLib.idle_add(self.file_queue.update_playing_state, False)

    def _play_next_file(self, file_path, index):
        """Helper to play the next file with proper UI updates."""
        if self.player.current_file == file_path and self.player.is_playing():
            return False

        same_file_as_active = file_path == self.active_audio_id

        # Reset visualizer/seekbar BEFORE load/play to avoid stale state
        if not same_file_as_active:
            logger.debug(f"Generating waveform for new file in queue: {file_path}")
            self.visualizer.set_waveform(None, 0)
            self.seekbar.set_duration(0)
            self.seekbar.set_position(0)
            self.active_audio_id = file_path

        if self.player.load(file_path, self.file_queue.track_metadata):
            self.file_queue.set_currently_playing(index)
            self.player.play()

            # Sync button state immediately to avoid race with stop() idle callbacks
            if hasattr(self, "pause_play_btn"):
                self.pause_play_btn.set_icon_name("media-playback-pause-symbolic")

            if not same_file_as_active:
                threading.Thread(
                    target=waveform.generate,
                    args=(
                        file_path,
                        self.converter,
                        self.visualizer,
                        self.file_markers,
                        self.zoom_control_box
                        if hasattr(self, "zoom_control_box")
                        else None,
                        self.file_queue.track_metadata,
                    ),
                    daemon=True,
                ).start()
            else:
                logger.debug(f"Same file, skipping waveform generation: {file_path}")

        return False

    # --- File activation and visualization ---

    def on_activate_file(self, file_path, index):
        """Handle file row activation (clicked) - show waveform, auto-play if already playing."""
        logger.info(f"File row activated: {file_path} at index {index}")
        was_playing = self.player.is_playing()
        self._load_file_for_visualization(file_path, index, play_audio=was_playing)

    def on_play_file(self, file_path, index):
        """Play a selected file."""
        self._load_file_for_visualization(file_path, index, play_audio=True)
        return False

    def _save_current_file_state(self):
        """Save markers for the currently active file before switching."""
        if self.active_audio_id and hasattr(self.visualizer, "get_marker_pairs"):
            current_markers = self.visualizer.get_marker_pairs()
            if current_markers:
                logger.debug(
                    f"Saving {len(current_markers)} markers for {self.active_audio_id}"
                )
                self.file_markers[self.active_audio_id] = current_markers
            elif self.active_audio_id in self.file_markers:
                logger.debug(f"Removing cached markers for {self.active_audio_id}")
                del self.file_markers[self.active_audio_id]

    def _prepare_visualizer_for_new_file(self, file_path, index):
        """Clears the old visualizer state and starts waveform generation for a new file."""
        logger.debug(f"Resetting visualizer for new file: {file_path}")
        self.visualizer.set_waveform(None, 0)
        self.seekbar.set_duration(0)
        self.seekbar.set_position(0)

        self.active_audio_id = file_path
        logger.debug(f"Setting active_audio_id to: {file_path}")

        if hasattr(self.file_queue, "set_active_file"):
            self.file_queue.set_active_file(index)

        markers_enabled = self.visualizer.markers_enabled
        self.visualizer.clear_all_markers()
        self.visualizer.markers_enabled = markers_enabled

        if self.cut_row.get_selected() > 0:
            threading.Thread(
                target=waveform.generate,
                args=(
                    file_path,
                    self.converter,
                    self.visualizer,
                    self.file_markers,
                    self.zoom_control_box,
                    self.file_queue.track_metadata,
                ),
                daemon=True,
            ).start()
        else:
            threading.Thread(
                target=waveform.activate_without_waveform,
                args=(
                    file_path,
                    self.converter,
                    self.visualizer,
                    self.file_markers,
                    self.zoom_control_box,
                    self.file_queue.track_metadata,
                ),
                daemon=True,
            ).start()

    def _load_file_for_visualization(self, file_path, index, play_audio=True):
        """Load a file for visualization and optional playback."""
        logger.info(f"_load_file_for_visualization: file={file_path}, play={play_audio}, current_playing={self.player.is_playing()}, current_file={self.player.current_file}")
        # Toggling play/pause on the currently loaded file
        if (
            play_audio
            and self.player.is_playing()
            and self.player.current_file == file_path
        ):
            self.player.pause()
            if hasattr(self.file_queue, "update_playing_state"):
                self.file_queue.update_playing_state(False)
            return

        # Stop any different file that might be playing
        if self.player.is_playing() and self.player.current_file != file_path:
            self.player.stop()

        # Save state of the previously active file
        self._save_current_file_state()

        # New file selected for visualization/playback
        if file_path != self.active_audio_id:
            self._prepare_visualizer_for_new_file(file_path, index)

        # Load the file into the player
        if self.player.load(file_path, self.file_queue.track_metadata):
            if play_audio:
                if hasattr(self.file_queue, "set_currently_playing"):
                    self.file_queue.set_currently_playing(index)
                self.player.play()
                # Sync UI immediately — on_file_loaded will confirm later
                if hasattr(self, "pause_play_btn"):
                    self.pause_play_btn.set_icon_name("media-playback-pause-symbolic")
                if hasattr(self.file_queue, "update_playing_state"):
                    self.file_queue.update_playing_state(True)

    # --- Time display ---

    def _update_time_display(self, position, duration):
        """Update the time display label (called via GLib.idle_add)."""
        if hasattr(self, "time_display_label"):
            duration_str = format_time_short(duration)
            self.time_display_label.set_label(duration_str)
        return False

    # --- Position tracking and segment transitions ---

    def on_player_position_updated(self, player, position, duration):
        """Handle position updates from player."""
        if duration > 0 and self.visualizer.duration != duration:
            logger.debug(f"Updating visualizer.duration: {self.visualizer.duration} -> {duration}")
            self.visualizer.duration = duration
        self.visualizer.set_position(position)
        self.seekbar.set_position(position)

        if duration > 0 and self.seekbar.duration != duration:
            self.seekbar.set_duration(duration)
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )

        GLib.idle_add(self._update_time_display, position, duration)
        GLib.idle_add(self._update_play_selection_button)

        # Handle segment transitions in Play Selection Only mode
        if self._playing_selection and self._selection_segments:
            if self._marker_dragging or self._is_transitioning_segment:
                return

            if self._current_segment_index >= len(self._selection_segments):
                logger.warning("Invalid segment index, stopping selection playback.")
                self.player.pause()
                self._playing_selection = False
                return

            start, stop = self._selection_segments[self._current_segment_index]
            TOLERANCE = 0.02  # 20ms

            if position > stop - TOLERANCE:
                logger.info(
                    f"Segment {self._current_segment_index + 1} end detected at {position:.3f}s (target: {stop:.3f}s). Transitioning."
                )
                self._is_transitioning_segment = True
                self._current_segment_index += 1
                GLib.idle_add(self._do_segment_transition_with_retry)
                return

            if position < start - TOLERANCE:
                logger.info(
                    f"Position {position:.3f}s is before segment start {start:.3f}s. Seeking to start."
                )
                self.player.seek(start)
                return

        # Auto-next detection fallback
        if duration > 0 and position >= duration - 0.2:
            if (
                not hasattr(self, "_end_of_track_handled")
                or not self._end_of_track_handled
            ):
                self._end_of_track_handled = True
                GLib.idle_add(self.on_playback_finished, player)
        elif hasattr(self, "_end_of_track_handled") and self._end_of_track_handled:
            self._end_of_track_handled = False

    def _do_segment_transition_with_retry(self, retry_count=0):
        """Execute segment transition with error handling and retry capability."""
        max_retries = 2

        if not self._playing_selection or not self._selection_segments:
            logger.debug("Segment transition cancelled - not in selection mode")
            self._is_transitioning_segment = False
            return False

        if self._current_segment_index < len(self._selection_segments):
            next_start, next_stop = self._selection_segments[
                self._current_segment_index
            ]
            logger.info(
                f"Transitioning to segment {self._current_segment_index + 1}/{len(self._selection_segments)}: {next_start:.3f}-{next_stop:.3f}"
            )

            try:
                current_pos = getattr(self.player, "_position", 0)
                if abs(current_pos - next_start) < 0.01:
                    logger.debug("Already at target position, skipping seek")
                    if not self.player.is_playing():
                        self.player.play()
                    self._is_transitioning_segment = False
                    return False

                success = self.player.seek(next_start)

                if not success and retry_count < max_retries:
                    logger.warning(
                        f"Segment transition seek failed, retry {retry_count + 1}/{max_retries}"
                    )
                    GLib.timeout_add(
                        100,
                        lambda: self._do_segment_transition_with_retry(retry_count + 1),
                    )
                    return False
                elif not success:
                    logger.error(
                        f"Segment transition failed after {max_retries} retries"
                    )
                    self._playing_selection = False
                    self._is_transitioning_segment = False
                    return False

                def ensure_playing():
                    if self._playing_selection and not self.player.is_playing():
                        logger.info("Resuming playback after segment transition")
                        self.player.play()
                    return False

                GLib.timeout_add(50, ensure_playing)
                self._is_transitioning_segment = False
                logger.debug("Transition lock released after successful seek.")

            except Exception as e:
                logger.error(f"Exception during segment transition: {e}")
                self._playing_selection = False
                self._is_transitioning_segment = False
                return False
        else:
            logger.info(
                f"All {len(self._selection_segments)} segments completed - pausing playback"
            )
            self._playing_selection = False
            self._is_transitioning_segment = False

            if self.player.is_playing():
                self.player.pause()

        return False

    def _do_segment_transition(self):
        """Execute segment transition on GTK main thread (legacy wrapper)."""
        return self._do_segment_transition_with_retry(0)

    def on_player_duration_changed(self, player, duration):
        """Handle duration changes from player."""
        if duration > 0:
            self.visualizer.duration = duration
            self.seekbar.set_duration(duration)

    # --- Seek handling ---

    def on_visualizer_seek(self, position, should_play):
        """Handle seek request from visualizer."""
        logger.info(
            f"MAIN_WINDOW: Received seek position={position:.6f}s, should_play={should_play}"
        )
        if not hasattr(self.player, "seek"):
            return

        # If marker is being dragged/resized, allow free seeking
        if self._marker_dragging:
            self.player.seek(position)
            self.visualizer.set_position(position)
            self.seekbar.set_position(position)
            return

        # If in Play Selection Only mode, validate position is in a segment
        if self._play_selection_mode and self._selection_segments:
            in_segment = False
            for i, (start, stop) in enumerate(self._selection_segments):
                if start <= position <= stop:
                    in_segment = True
                    self._current_segment_index = i
                    logger.debug(f"Seek action sets current segment to index {i}")
                    break

            if not in_segment:
                after_any_selection = False
                for start, stop in self._selection_segments:
                    if position > stop:
                        after_any_selection = True
                        break

                if after_any_selection:
                    next_segment = None
                    for i, (start, stop) in enumerate(self._selection_segments):
                        if start > position:
                            next_segment = i
                            break

                    if next_segment is not None:
                        position = self._selection_segments[next_segment][0]
                        self._current_segment_index = next_segment
                        logger.info(
                            f"Seek after selection, jumping to next segment {next_segment} at {position:.3f}"
                        )
                    else:
                        position = self._selection_segments[0][0]
                        self._current_segment_index = 0
                        logger.info(
                            f"No segment after, jumping to first segment at {position:.3f}"
                        )
                else:
                    nearest_segment = None
                    min_distance = float("inf")

                    for i, (start, stop) in enumerate(self._selection_segments):
                        if position < start:
                            distance = start - position
                        elif position > stop:
                            distance = position - stop
                        else:
                            distance = 0

                        if distance < min_distance:
                            min_distance = distance
                            nearest_segment = i

                    if nearest_segment is not None:
                        position = self._selection_segments[nearest_segment][0]
                        self._current_segment_index = nearest_segment
                        logger.info(
                            f"Seek before selections, jumping to nearest segment {nearest_segment} at {position:.3f}"
                        )

            self._is_transitioning_segment = False

        self.player.seek(position)
        self.visualizer.set_position(position)
        self.seekbar.set_position(position)

        if should_play:
            self._on_pause_play_clicked(None)

    # --- Marker updates ---

    def _on_markers_updated(self, markers):
        """Handle marker updates from visualizer."""
        if self._play_selection_mode:
            self._load_selection_segments()

            if self._playing_selection and self._selection_segments:
                current_position = (
                    self.player._position if hasattr(self.player, "_position") else 0
                )

                found_index = -1
                for i, (start, stop) in enumerate(self._selection_segments):
                    if start <= current_position <= stop:
                        found_index = i
                        break

                if found_index >= 0:
                    self._current_segment_index = found_index
                    logger.debug(
                        f"Updated current segment to {found_index} after marker change"
                    )
                else:
                    for i, (start, stop) in enumerate(self._selection_segments):
                        if start > current_position:
                            self._current_segment_index = i
                            logger.debug(
                                f"Jumped to next segment {i} after marker change"
                            )
                            break
                    else:
                        logger.info("No valid segment after marker change, stopping")
                        self._playing_selection = False
                        self.player.stop()

    def _on_marker_drag_state_changed(self, is_dragging):
        """Handle marker drag state changes."""
        self._marker_dragging = is_dragging
        if is_dragging:
            logger.info(
                "Marker dragging started - DISABLING segment boundary checks"
            )
        else:
            logger.info(
                "Marker dragging ended - RE-ENABLING segment boundary checks"
            )

            if self._playing_selection and self._selection_segments:
                current_position = (
                    self.player._position if hasattr(self.player, "_position") else 0
                )

                if hasattr(self, "_last_transition_times"):
                    self._last_transition_times.clear()
                    logger.debug("Cleared transition tracking after drag end")

                found_segment = -1
                for i, (start, stop) in enumerate(self._selection_segments):
                    if start <= current_position <= stop:
                        found_segment = i
                        break

                if found_segment >= 0:
                    self._current_segment_index = found_segment
                    logger.debug(
                        f"Position {current_position:.3f}s is in segment {found_segment}"
                    )
                else:
                    if current_position < self._selection_segments[0][0]:
                        logger.info(
                            f"Position {current_position:.3f}s is before segments, seeking to first segment"
                        )
                        self._current_segment_index = 0
                        start, _ = self._selection_segments[0]
                        self._marker_dragging = True
                        GLib.idle_add(
                            lambda: (
                                self.player.seek(start)
                                if self.player.is_playing()
                                else None,
                                setattr(self, "_marker_dragging", False),
                            )[0]
                        )
                    elif current_position > self._selection_segments[-1][1]:
                        logger.info(
                            f"Position {current_position:.3f}s is after all segments, stopping"
                        )
                        self._playing_selection = False
                        GLib.idle_add(
                            lambda: (
                                self.player.stop() if self.player.is_playing() else None
                            )
                        )
                    else:
                        for i, (start, stop) in enumerate(self._selection_segments):
                            if start > current_position:
                                logger.info(
                                    f"Position {current_position:.3f}s is in gap, seeking to next segment {i}"
                                )
                                self._current_segment_index = i
                                self._marker_dragging = True

                                def do_seek_and_reenable():
                                    if self.player.is_playing():
                                        self.player.seek(start)
                                    GLib.timeout_add(
                                        150,
                                        lambda: (
                                            setattr(self, "_marker_dragging", False),
                                            False,
                                        )[1],
                                    )
                                    return False

                                GLib.idle_add(do_seek_and_reenable)
                                break

    # --- Player state ---

    def on_player_state_changed(self, player, is_playing):
        """Handle player state changes to update UI."""
        # Use actual player state to avoid stale callbacks from stop()/play() race
        actual_state = player.is_playing()
        logger.info(f"State changed: reported={is_playing}, actual={actual_state}")
        is_playing = actual_state

        if hasattr(self.file_queue, "update_playing_state"):
            self.file_queue.update_playing_state(is_playing)

        if hasattr(self, "pause_play_btn"):
            if is_playing:
                self.pause_play_btn.set_icon_name("media-playback-pause-symbolic")
            else:
                self.pause_play_btn.set_icon_name("media-playback-start-symbolic")

    # --- Play/Pause/Next/Previous controls ---

    def _on_pause_play_clicked(self, button):
        """Handle pause/play button click."""
        if self.player.is_playing():
            self.player.pause()
            if self._playing_selection:
                self._playing_selection = False
        else:
            active_index = None
            if self.active_audio_id:
                for i, file_path in enumerate(self.file_queue.files):
                    if file_path == self.active_audio_id:
                        active_index = i
                        break

            if (
                self.active_audio_id
                and self.player.current_file != self.active_audio_id
            ):
                logger.info(f"Loading active file for playback: {self.active_audio_id}")
                self.player.load(self.active_audio_id, self.file_queue.track_metadata)

                if active_index is not None and hasattr(
                    self.file_queue, "set_currently_playing"
                ):
                    self.file_queue.set_currently_playing(active_index)
            elif active_index is not None and hasattr(
                self.file_queue, "set_currently_playing"
            ):
                self.file_queue.set_currently_playing(active_index)

            if self._play_selection_mode:
                self._start_selection_playback()

            self.player.play()

    def _on_previous_audio_clicked(self):
        """Handle previous audio button click - go to previous file in queue."""
        if not self.file_queue.files:
            return

        current_index = None
        if (
            hasattr(self.file_queue, "currently_playing_index")
            and self.file_queue.currently_playing_index is not None
        ):
            current_index = self.file_queue.currently_playing_index
        elif (
            hasattr(self.file_queue, "active_file_index")
            and self.file_queue.active_file_index is not None
        ):
            current_index = self.file_queue.active_file_index
        else:
            current_index = len(self.file_queue.files)

        prev_index = (current_index - 1) % len(self.file_queue.files)

        prev_file = self.file_queue.files[prev_index]
        logger.info(f"Going to previous audio: {prev_file}")
        self._load_file_for_visualization(prev_file, prev_index, play_audio=True)

    def _on_next_audio_clicked(self):
        """Handle next audio button click - go to next file in queue."""
        if not self.file_queue.files:
            return

        current_index = None
        if (
            hasattr(self.file_queue, "currently_playing_index")
            and self.file_queue.currently_playing_index is not None
        ):
            current_index = self.file_queue.currently_playing_index
        elif (
            hasattr(self.file_queue, "active_file_index")
            and self.file_queue.active_file_index is not None
        ):
            current_index = self.file_queue.active_file_index
        else:
            current_index = -1

        next_index = (current_index + 1) % len(self.file_queue.files)

        next_file = self.file_queue.files[next_index]
        logger.info(f"Going to next audio: {next_file}")
        self._load_file_for_visualization(next_file, next_index, play_audio=True)

    # --- Selection playback ---

    def _on_play_selection_switch_toggled(self, button):
        """Handle play selection toggle button state change."""
        active = button.get_active()
        self._play_selection_mode = active

        logger.info(f"Play Selection Only mode: {'ENABLED' if active else 'DISABLED'}")

        if active:
            if self.player.is_playing():
                self._start_selection_playback()
        else:
            if self._playing_selection:
                self._playing_selection = False
                logger.info("Selection playback mode stopped")

    def _on_auto_advance_switch_toggled(self, button):
        """Handle auto-advance toggle button state change."""
        is_active = button.get_active()
        self.app.config.set("auto_advance_enabled", is_active)
        logger.info(f"Auto-advance mode: {'ENABLED' if is_active else 'DISABLED'}")

    def _load_selection_segments(self):
        """Load segments from markers."""
        order_by_number = self.cut_row.get_selected() == 2
        marker_pairs = self.visualizer.get_ordered_marker_pairs(order_by_number)

        segments = []
        for marker in marker_pairs:
            if marker.get("start") is not None and marker.get("stop") is not None:
                start = marker["start"]
                stop = marker["stop"]
                if start > stop:
                    start, stop = stop, start
                segments.append((start, stop))

        self._selection_segments = segments
        return segments

    def _start_selection_playback(self):
        """Prepare for selection playback by setting state and seeking."""
        segments = self._load_selection_segments()
        if not segments:
            logger.warning("Cannot start selection playback - no segments available.")
            self._playing_selection = False
            return

        self._playing_selection = True
        self._is_transitioning_segment = False

        if self._current_segment_index >= len(segments):
            self._current_segment_index = 0

        start, stop = segments[self._current_segment_index]
        logger.info(
            f"Preparing selection playback from segment {self._current_segment_index + 1}/{len(segments)}: {start:.3f}-{stop:.3f}"
        )

        self.player.seek(start)

    def _update_play_selection_button(self):
        """Update Play Selection switch state based on markers."""
        if not hasattr(self, "play_selection_switch"):
            return False

        has_complete_pair = False
        for marker in self.visualizer.marker_pairs:
            if marker.get("start") is not None and marker.get("stop") is not None:
                has_complete_pair = True
                break

        if not has_complete_pair and self.play_selection_switch.get_active():
            self.play_selection_switch.set_active(False)

        return False

    # --- File removal ---

    def _on_file_removed(self, file_id):
        """Handle when a file is removed from the queue."""
        if file_id == self.active_audio_id:
            logger.info(
                f"Active file {file_id} was removed, clearing visualizer and player"
            )
            self.visualizer.clear_waveform()
            self.active_audio_id = None

        if hasattr(self.player, "current_file"):
            player_file = self.player.current_file
            player_actual_file = getattr(self.player, "current_actual_file", None)

            if player_file == file_id or (
                player_actual_file and player_actual_file == file_id
            ):
                logger.info("Player had removed file loaded, stopping and clearing")
                self.player.stop()
                self.player.current_file = None
                if hasattr(self.player, "current_actual_file"):
                    self.player.current_actual_file = None
                logger.debug("Stopped and unloaded player")
