"""The window's playback: the shown file, auto-advance and marked-part playback."""

import logging

from app.audio.media import complement

logger = logging.getLogger(__name__)


class PlaybackControllerMixin:
    """Mixin providing all playback control logic for MainWindow."""

    def on_playback_finished(self, player):
        """Play the next file when auto-advance is on."""

        if not self.auto_advance_switch.get_active():
            self._playback_sources.idle(self.file_queue.update_playing_state, False)
            return

        current_index = self.file_queue.currently_playing_index
        if current_index is None:
            return

        files = self.file_queue.get_files()
        if not files:
            return

        next_index = current_index + 1
        if next_index < len(files):
            next_file = files[next_index]
            self._playback_sources.later(
                300, self._play_next_file, next_file, next_index
            )
        else:
            self._playback_sources.idle(self.file_queue.update_playing_state, False)

    def _play_next_file(self, file_path, index):
        """Helper to play the next file with proper UI updates."""
        if self._closed or file_path not in self.file_queue.files:
            return
        index = self.file_queue.files.index(file_path)
        if self.player.current_file == file_path and self.player.is_playing():
            return
        if file_path != self.active_audio_id:
            # The marks on screen belong to the file that just ended.
            self._save_current_file_state()
            self._prepare_visualizer_for_new_file(file_path, index)
        if self.player.load(file_path, self.file_queue.track_metadata):
            self.file_queue.set_currently_playing(index)
            self.player.play()
            # Sync button state immediately to avoid race with stop() idle callbacks
            self.pause_play_btn.set_icon_name("media-playback-pause-symbolic")

    def on_activate_file(self, file_path, index):
        """Show the row's waveform; keep playing if playback was running."""
        was_playing = self.player.is_playing()
        self._load_file_for_visualization(file_path, index, play_audio=was_playing)

    def on_play_file(self, file_path, index):
        """Play a selected file."""
        self._load_file_for_visualization(file_path, index, play_audio=True)
        return False

    def _save_current_file_state(self):
        """Save markers for the currently active file before switching."""
        if self.active_audio_id:
            current_markers = self.visualizer.get_marker_pairs()
            if current_markers:
                self.file_markers[self.active_audio_id] = current_markers
            elif self.active_audio_id in self.file_markers:
                del self.file_markers[self.active_audio_id]

    def _prepare_visualizer_for_new_file(self, file_path, index):
        """Clears the old visualizer state and starts waveform generation for a new file."""
        self._playback_sources.clear()
        self._playing_selection = False
        self._is_transitioning_segment = False
        self.waveform_generator.cancel()
        self.visualizer.set_waveform(None, 0)
        self.seekbar.set_duration(0)
        self.seekbar.set_position(0)

        self.active_audio_id = file_path

        self.file_queue.set_active_file(index)

        self.visualizer.clear_all_markers()
        self._request_waveform(file_path)

    def _load_file_for_visualization(self, file_path, index, play_audio=True):
        """Load a file for visualization and optional playback."""
        if self._closed or file_path not in self.file_queue.files:
            return
        index = self.file_queue.files.index(file_path)
        if (
            self.file_queue.file_rows[index].source_path
            in self.file_queue._pending_probes
        ):
            return
        # Toggling play/pause on the currently loaded file
        if (
            play_audio
            and self.player.is_playing()
            and self.player.current_file == file_path
        ):
            self.player.pause()
            self.file_queue.update_playing_state(False)
            return

        if self.player.is_playing() and self.player.current_file != file_path:
            self.player.stop()

        # The marks on screen belong to the file being left.
        self._save_current_file_state()

        # New file selected for visualization/playback
        if file_path != self.active_audio_id:
            self._prepare_visualizer_for_new_file(file_path, index)

        if (self.player.load(file_path, self.file_queue.track_metadata)) and (
            play_audio
        ):
            self.file_queue.set_currently_playing(index)
            self.player.play()
            # Sync UI immediately; the player's state callback confirms later.
            self.pause_play_btn.set_icon_name("media-playback-pause-symbolic")
            self.file_queue.update_playing_state(True)

    def on_player_position_updated(self, player, position, duration):
        if duration > 0 and self.visualizer.duration != duration:
            self.visualizer.duration = duration
        self.visualizer.set_position(position)
        self.seekbar.set_position(position)

        if duration > 0 and self.seekbar.duration != duration:
            self.seekbar.set_duration(duration)
        self.seekbar.set_zoom_viewport(
            self.visualizer.zoom_level, self.visualizer.viewport_offset
        )

        self._update_play_selection_button()

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
                self._is_transitioning_segment = True
                self._current_segment_index += 1
                self._playback_sources.idle(self._do_segment_transition)
                return

            if position < start - TOLERANCE:
                self.player.seek(start)
                return

    def _do_segment_transition(self):
        """Seek to the next selected segment, or pause after the last one."""
        if not self._playing_selection or not self._selection_segments:
            self._is_transitioning_segment = False
            return False

        if self._current_segment_index < len(self._selection_segments):
            next_start = self._selection_segments[self._current_segment_index][0]

            try:
                if abs(self.player._position - next_start) < 0.01:
                    if not self.player.is_playing():
                        self.player.play()
                    self._is_transitioning_segment = False
                    return False

                self.player.seek(next_start)

                def ensure_playing():
                    if self._playing_selection and not self.player.is_playing():
                        self.player.play()
                    return False

                self._playback_sources.later(50, ensure_playing)
                self._is_transitioning_segment = False

            except (ValueError, TypeError, RuntimeError) as e:
                logger.error("Exception during segment transition: %s", e)
                self._playing_selection = False
                self._is_transitioning_segment = False
                return False
        else:
            self._playing_selection = False
            self._is_transitioning_segment = False

            if self.player.is_playing():
                self.player.pause()

        return False

    def on_player_duration_changed(self, player, duration):
        if duration > 0:
            self.visualizer.duration = duration
            self.seekbar.set_duration(duration)

    def on_visualizer_seek(self, position, should_play):
        if self._marker_dragging:
            self.player.seek(position)
            self.visualizer.set_position(position)
            self.seekbar.set_position(position)
            return

        if self._play_selection_mode and self._selection_segments:
            in_segment = False
            for i, (start, stop) in enumerate(self._selection_segments):
                if start <= position <= stop:
                    in_segment = True
                    self._current_segment_index = i
                    break

            if not in_segment:
                after_any_selection = any(
                    position > stop for _start, stop in self._selection_segments
                )

                if after_any_selection:
                    next_segment = None
                    for i, (start, _stop) in enumerate(self._selection_segments):
                        if start > position:
                            next_segment = i
                            break

                    if next_segment is not None:
                        position = self._selection_segments[next_segment][0]
                        self._current_segment_index = next_segment
                    else:
                        position = self._selection_segments[0][0]
                        self._current_segment_index = 0
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

            self._is_transitioning_segment = False

        self.player.seek(position)
        self.visualizer.set_position(position)
        self.seekbar.set_position(position)

        if should_play:
            self._on_pause_play_clicked(None)

    def _on_markers_updated(self, markers):
        self._update_cut_rows()
        self.pause_panel.markers_changed(markers)
        if self._play_selection_mode:
            self._load_selection_segments()

            if self._playing_selection and self._selection_segments:
                current_position = self.player._position

                found_index = -1
                for i, (start, stop) in enumerate(self._selection_segments):
                    if start <= current_position <= stop:
                        found_index = i
                        break

                if found_index >= 0:
                    self._current_segment_index = found_index
                else:
                    for i, (start, _stop) in enumerate(self._selection_segments):
                        if start > current_position:
                            self._current_segment_index = i
                            break
                    else:
                        self._playing_selection = False
                        self.player.stop()

    def _on_marker_drag_state_changed(self, is_dragging):
        """After a drag, keep playing the part under the playhead, or the next."""
        self._marker_dragging = is_dragging
        # Segment boundaries are not enforced while a mark is being dragged.
        segments = self._selection_segments
        if is_dragging or not (self._playing_selection and segments):
            return
        position = self.player._position
        inside = next(
            (
                i
                for i, (start, stop) in enumerate(segments)
                if start <= position <= stop
            ),
            None,
        )
        if inside is not None:
            self._current_segment_index = inside
            return
        if position > segments[-1][1]:
            self._playing_selection = False
            self._playback_sources.idle(self._stop_if_playing)
            return
        index = next(i for i, (start, _stop) in enumerate(segments) if start > position)
        self._current_segment_index = index
        self._marker_dragging = True
        self._playback_sources.idle(self._seek_after_drag, segments[index][0])

    def _stop_if_playing(self):
        if self.player.is_playing():
            self.player.stop()

    def _seek_after_drag(self, target):
        if self.player.is_playing():
            self.player.seek(target)
        # The seek's own position update must not count as leaving the part.
        self._playback_sources.later(150, setattr, self, "_marker_dragging", False)

    def on_player_state_changed(self, player, is_playing):
        # Use actual player state to avoid stale callbacks from stop()/play() race
        actual_state = player.is_playing()
        is_playing = actual_state

        self.file_queue.update_playing_state(is_playing)
        self.pause_play_btn.set_icon_name(
            "media-playback-pause-symbolic"
            if is_playing
            else "media-playback-start-symbolic"
        )

    def _on_pause_play_clicked(self, button):
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
                and not self.player.load(
                    self.active_audio_id, self.file_queue.track_metadata
                )
            ):
                return

            if active_index is not None:
                self.file_queue.set_currently_playing(active_index)

            if self._play_selection_mode:
                self._start_selection_playback()

            self.player.play()

    def _on_previous_audio_clicked(self):
        if not self.file_queue.files:
            return

        current_index = self.file_queue.currently_playing_index
        if current_index is None:
            current_index = self.file_queue.active_file_index
        if current_index is None:
            current_index = len(self.file_queue.files)

        prev_index = (current_index - 1) % len(self.file_queue.files)

        prev_file = self.file_queue.files[prev_index]
        self._load_file_for_visualization(prev_file, prev_index, play_audio=True)

    def _on_next_audio_clicked(self):
        if not self.file_queue.files:
            return

        current_index = self.file_queue.currently_playing_index
        if current_index is None:
            current_index = self.file_queue.active_file_index
        if current_index is None:
            current_index = -1

        next_index = (current_index + 1) % len(self.file_queue.files)

        next_file = self.file_queue.files[next_index]
        self._load_file_for_visualization(next_file, next_index, play_audio=True)

    def _on_play_selection_switch_toggled(self, button):
        active = button.get_active()
        self._play_selection_mode = active

        if active:
            if self.player.is_playing():
                self._start_selection_playback()
        else:
            if self._playing_selection:
                self._playing_selection = False

    def _on_auto_advance_switch_toggled(self, button):
        is_active = button.get_active()
        self.app.config.set("auto_advance_enabled", is_active)

    def _load_selection_segments(self):
        """Load segments from markers, or what remains when they are removed."""
        order_by_number = self.cut_order_row.get_selected() == 1
        marker_pairs = self.visualizer.get_ordered_marker_pairs(order_by_number)
        if self.removes_marked():
            marker_pairs = complement(marker_pairs, self.visualizer.duration)

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

        start = segments[self._current_segment_index][0]

        self.player.seek(start)

    def _update_play_selection_button(self):
        has_complete_pair = False
        for marker in self.visualizer.marker_pairs:
            if marker.get("start") is not None and marker.get("stop") is not None:
                has_complete_pair = True
                break

        if not has_complete_pair and self.play_selection_switch.get_active():
            self.play_selection_switch.set_active(False)

        return False

    def _on_file_removed(self, file_id):
        self.file_markers.pop(file_id, None)
        if file_id == self.active_audio_id:
            self.visualizer.clear_waveform()
            self.visualizer.clear_all_markers()
            self.active_audio_id = None
            self.pause_panel.sync()
        self._update_cut_rows()

        if file_id in (self.player.current_file, self.player.current_actual_file):
            self.player.stop()
            self.player.current_file = None
            self.player.current_actual_file = None
