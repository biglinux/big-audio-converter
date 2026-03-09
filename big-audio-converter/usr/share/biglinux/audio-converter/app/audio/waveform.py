"""
Waveform generation module for audio visualization.
"""

import logging
import os
import subprocess

import numpy as np
from gi.repository import GLib

logger = logging.getLogger(__name__)


class WaveformGenerator:
    """Encapsulates waveform generation with proper process lifecycle management."""

    # Target maximum number of samples to generate (~60 MB max memory).
    TARGET_MAX_SAMPLES = 2_000_000
    MAX_RATE = 44100
    MIN_RATE = 200

    def __init__(self):
        self._current_process = None

    def _cancel_current(self):
        """Cancel any running waveform generation process."""
        if self._current_process is not None:
            try:
                if self._current_process.poll() is None:
                    logger.info("Terminating previous waveform generation process")
                    self._current_process.terminate()
                    try:
                        self._current_process.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        logger.warning("Previous process didn't terminate, killing it")
                        self._current_process.kill()
                        self._current_process.wait()
            except Exception as e:
                logger.error(f"Error terminating previous process: {e}")
            self._current_process = None

    def _resolve_path(self, file_path, track_metadata):
        """Resolve virtual track paths to actual file paths."""
        actual_file_path = file_path
        audio_stream_index = None

        if "::" in file_path and track_metadata and file_path in track_metadata:
            metadata = track_metadata[file_path]
            actual_file_path = metadata["source_video"]
            audio_stream_index = metadata["track_index"]
            logger.info(f"Resolved track {audio_stream_index} from {actual_file_path}")

        return actual_file_path, audio_stream_index

    def _get_duration(self, ffprobe_path, actual_file_path):
        """Get audio duration using ffprobe."""
        probe_cmd = [
            ffprobe_path,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            actual_file_path,
        ]
        result = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=5)
        duration = float(result.stdout.strip())
        logger.info(
            f"ffprobe duration={duration:.6f}s for {os.path.basename(actual_file_path)}"
        )
        return duration

    def generate(
        self,
        file_path,
        converter_instance,
        visualizer,
        file_markers=None,
        zoom_control_box=None,
        track_metadata=None,
    ):
        """Generate waveform data using a dynamically-calculated resolution."""
        self._cancel_current()

        def set_loading():
            visualizer.set_loading(True, "Generating waveform...")
            return False

        GLib.idle_add(set_loading)

        try:
            actual_file_path, audio_stream_index = self._resolve_path(
                file_path, track_metadata
            )

            if not os.path.exists(actual_file_path):
                logger.error(f"File not found: {actual_file_path}")
                GLib.idle_add(
                    lambda: (
                        (
                            visualizer.set_loading(False),
                            visualizer.set_waveform(None, 0),
                        )
                        or False
                    )
                )
                return

            logger.info(f"Generating waveform for: {actual_file_path}")
            ffmpeg_path = getattr(converter_instance, "ffmpeg_path", "ffmpeg")
            ffprobe_path = getattr(converter_instance, "ffprobe_path", "ffprobe")

            try:
                duration = self._get_duration(ffprobe_path, actual_file_path)
            except Exception as e:
                logger.error(f"Failed to get duration: {e}")
                GLib.idle_add(
                    lambda: (
                        (
                            visualizer.set_loading(False),
                            visualizer.set_waveform(None, 0),
                        )
                        or False
                    )
                )
                return

            # Dynamic sample rate calculation
            if duration > 0:
                ideal_rate = self.TARGET_MAX_SAMPLES / duration
                target_rate = int(max(self.MIN_RATE, min(self.MAX_RATE, ideal_rate)))
            else:
                target_rate = self.MAX_RATE

            logger.info(
                f"File duration {duration:.1f}s. Dynamic rate: {target_rate} Hz."
            )

            cmd = [
                ffmpeg_path,
                "-vn",
                "-sn",
                "-v",
                "error",
                "-i",
                actual_file_path,
            ]

            if audio_stream_index is not None:
                cmd.extend(["-map", f"0:{audio_stream_index}"])

            # Use left channel only (pan filter) instead of -ac 1 downmix
            # to avoid phase cancellation in stereo files with inverted channels
            cmd.extend([
                "-af",
                "pan=mono|c0=c0",
                "-f",
                "f32le",
                "-ar",
                str(target_rate),
                "-",
            ])

            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            self._current_process = process

            chunk_size = 65536
            data_list = []

            while True:
                chunk_bytes = process.stdout.read(chunk_size)
                if not chunk_bytes:
                    break
                chunk = np.frombuffer(chunk_bytes, dtype=np.float32)
                if len(chunk) > 0:
                    data_list.append(chunk)

            process.wait()
            self._current_process = None

            if not data_list:
                logger.error("No waveform data generated")
                GLib.idle_add(
                    lambda: (
                        (
                            visualizer.set_loading(False),
                            visualizer.set_waveform(None, 0),
                        )
                        or False
                    )
                )
                return

            waveform_data = np.concatenate(data_list)
            del data_list

            if (
                len(waveform_data) < 10
                or np.isnan(waveform_data).any()
                or np.isinf(waveform_data).any()
            ):
                logger.error("Invalid waveform data")
                del waveform_data
                GLib.idle_add(
                    lambda: (
                        (
                            visualizer.set_loading(False),
                            visualizer.set_waveform(None, 0),
                        )
                        or False
                    )
                )
                return

            max_val = np.max(np.abs(waveform_data))
            if max_val > 0:
                waveform_data = waveform_data / max_val

            total_size_kb = len(waveform_data) * 4 / 1024
            logger.info(
                f"Generated {len(waveform_data)} samples ({total_size_kb:.1f} KB)"
            )

            waveform_payload = {
                "levels": [waveform_data],
                "rates": [target_rate],
                "zoom_thresholds": [1.0],
            }

            def update_visualizer():
                visualizer.set_waveform(waveform_payload, duration)
                if (
                    file_markers
                    and file_path in file_markers
                    and visualizer.markers_enabled
                ):
                    visualizer.restore_markers(file_markers[file_path])
                return False

            GLib.idle_add(update_visualizer)

        except Exception as e:
            logger.error(f"Error generating waveform: {str(e)}")
            self._current_process = None

            GLib.idle_add(
                lambda: (
                    (visualizer.set_loading(False), visualizer.set_waveform(None, 0))
                    or False
                )
            )

    def activate_without_waveform(
        self,
        file_path,
        converter_instance,
        visualizer,
        file_markers=None,
        zoom_control_box=None,
        track_metadata=None,
    ):
        """Activate a file in the visualizer without generating waveform data."""
        try:
            actual_file_path, _ = self._resolve_path(file_path, track_metadata)

            if not os.path.exists(actual_file_path):
                logger.error(f"File not found: {actual_file_path}")
                GLib.idle_add(lambda: visualizer.set_waveform(None, 0) or False)
                return

            logger.info(f"Activating file without waveform: {actual_file_path}")
            ffprobe_path = getattr(converter_instance, "ffprobe_path", "ffprobe")

            try:
                duration = self._get_duration(ffprobe_path, actual_file_path)
            except Exception as e:
                logger.error(f"Failed to get duration: {e}")
                GLib.idle_add(lambda: visualizer.set_waveform(None, 0) or False)
                return

            def update_visualizer():
                visualizer.set_waveform(None, duration)
                if (
                    file_markers
                    and file_path in file_markers
                    and visualizer.markers_enabled
                ):
                    visualizer.restore_markers(file_markers[file_path])
                return False

            GLib.idle_add(update_visualizer)

        except Exception as e:
            logger.error(f"Error activating file without waveform: {str(e)}")
            GLib.idle_add(lambda: visualizer.set_waveform(None, 0) or False)


# Module-level singleton for backward compatibility
_generator = WaveformGenerator()
generate = _generator.generate
activate_without_waveform = _generator.activate_without_waveform
