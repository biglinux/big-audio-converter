"""
Waveform generation module for audio visualization.
"""

import os
import subprocess
import logging
from gi.repository import GLib

logger = logging.getLogger(__name__)

# Global variable to track the current waveform generation process
_current_waveform_process = None


def generate(
    file_path, converter_instance, visualizer, file_markers=None, zoom_control_box=None, track_metadata=None
):
    """Generate waveform data using a single, dynamically-calculated resolution.

    This method calculates the optimal sample rate based on the audio file's
    duration to stay within a target memory budget. It aims for a maximum
    number of data points, ensuring that very long files remain memory-efficient
    while short files get maximum detail for precise zooming.

    The sample rate is determined by the formula:
    rate = TARGET_MAX_SAMPLES / duration_in_seconds

    This rate is then clamped between a minimum and maximum value to ensure
    both quality and performance.

    Args:
        file_path: Path to the audio file (may be virtual path for tracks)
        converter_instance: AudioConverter instance for accessing ffmpeg_path
        visualizer: Visualizer widget to update with waveform data
        file_markers: Dictionary of file markers (optional)
        zoom_control_box: Zoom control box widget (optional)
        track_metadata: Dictionary of track metadata for video files (optional)
    """
    # Cancel any previous waveform generation process
    global _current_waveform_process
    if _current_waveform_process is not None:
        try:
            if _current_waveform_process.poll() is None:  # Process is still running
                logger.info("Terminating previous waveform generation process")
                _current_waveform_process.terminate()
                try:
                    _current_waveform_process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    logger.warning("Previous process didn't terminate, killing it")
                    _current_waveform_process.kill()
                    _current_waveform_process.wait()
        except Exception as e:
            logger.error(f"Error terminating previous process: {e}")
        _current_waveform_process = None

    # Set loading state before starting generation
    def set_loading():
        visualizer.set_loading(True, "Generating waveform...")
        return False
    
    GLib.idle_add(set_loading)
    
    # --- Configuration for Dynamic Rate Calculation ---
    # Target maximum number of samples to generate, controlling memory usage.
    # 15 million samples * 4 bytes/sample = ~60 MB max memory footprint.
    TARGET_MAX_SAMPLES = 15_000_000
    # Maximum rate for very short files to prevent excessive processing.
    MAX_RATE = 44100
    # Minimum rate for very long files to ensure visual usefulness.
    MIN_RATE = 500
    # ----------------------------------------------------

    try:
        import numpy as np
        
        # Check if this is a track extraction
        actual_file_path = file_path
        audio_stream_index = None
        
        if '::' in file_path and track_metadata and file_path in track_metadata:
            # Extract actual file path and track index
            metadata = track_metadata[file_path]
            actual_file_path = metadata['source_video']
            audio_stream_index = metadata['track_index']
            logger.info(f"Generating waveform for track {audio_stream_index} from {actual_file_path}")

        if not os.path.exists(actual_file_path):
            logger.error(f"File not found: {actual_file_path}")

            def clear_on_error():
                visualizer.set_loading(False)
                visualizer.set_waveform(None, 0)
                if zoom_control_box:
                    zoom_control_box.set_visible(False)
                return False

            GLib.idle_add(clear_on_error)
            return

        logger.info(f"Generating waveform for: {actual_file_path}")

        # Get FFmpeg path
        ffmpeg_path = getattr(converter_instance, "ffmpeg_path", "ffmpeg")
        ffprobe_path = getattr(converter_instance, "ffprobe_path", "ffprobe")

        # Use ffprobe to get duration without loading file
        try:
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
            result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=5
            )
            duration = float(result.stdout.strip())
            logger.info(f"🔍 WAVEFORM: ffprobe returned duration={duration:.6f}s for {os.path.basename(actual_file_path)}")
        except Exception as e:
            logger.error(f"Failed to get duration: {e}")

            def clear_on_error():
                visualizer.set_loading(False)
                visualizer.set_waveform(None, 0)
                if zoom_control_box:
                    zoom_control_box.set_visible(False)
                return False

            GLib.idle_add(clear_on_error)
            return

        # --- Dynamic Sample Rate Calculation ---
        if duration > 0:
            ideal_rate = TARGET_MAX_SAMPLES / duration
            # Clamp the rate between MIN_RATE and MAX_RATE
            target_rate = int(max(MIN_RATE, min(MAX_RATE, ideal_rate)))
        else:
            target_rate = MAX_RATE  # Default for zero-duration files

        logger.info(
            f"File duration {duration:.1f}s. Calculated dynamic rate: {target_rate} Hz."
        )

        # Extract audio at the dynamically calculated rate
        cmd = [
            ffmpeg_path,
            "-vn",
            "-sn",
            "-v",
            "error",
            "-i",
            actual_file_path,
        ]
        
        # If extracting a specific track, add -map option
        if audio_stream_index is not None:
            cmd.extend(["-map", f"0:{audio_stream_index}"])
            logger.info(f"Extracting waveform from audio stream {audio_stream_index}")
        
        cmd.extend([
            "-f",
            "f32le",  # 32-bit float output
            "-ac",
            "1",  # Mono
            "-ar",
            str(target_rate),
            "-",  # Output to stdout
        ])

        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Store this process as the current one
        _current_waveform_process = process

        # Read in chunks
        chunk_size = 16384  # 16KB chunks
        data_list = []

        while True:
            chunk_bytes = process.stdout.read(chunk_size)
            if not chunk_bytes:
                break

            chunk = np.frombuffer(chunk_bytes, dtype=np.float32)
            if len(chunk) > 0:
                data_list.append(chunk)

        process.wait()

        # Clear the global process reference after completion
        _current_waveform_process = None

        if not data_list:
            logger.error("No waveform data generated")

            def clear_on_error():
                visualizer.set_loading(False)
                visualizer.set_waveform(None, 0)
                if zoom_control_box:
                    zoom_control_box.set_visible(False)
                return False

            GLib.idle_add(clear_on_error)
            return

        # Combine and validate the single waveform data array
        waveform_data = np.concatenate(data_list)
        
        # Clear data_list to release memory
        del data_list

        if (
            len(waveform_data) < 100
            or np.isnan(waveform_data).any()
            or np.isinf(waveform_data).any()
        ):
            logger.error("Invalid waveform data")
            del waveform_data  # Clean up before returning

            def clear_on_error():
                visualizer.set_loading(False)
                visualizer.set_waveform(None, 0)
                if zoom_control_box:
                    zoom_control_box.set_visible(False)
                return False

            GLib.idle_add(clear_on_error)
            return

        # Normalize to -1 to 1 range
        max_val = np.max(np.abs(waveform_data))
        if max_val > 0:
            waveform_data = waveform_data / max_val

        total_size_kb = len(waveform_data) * 4 / 1024
        logger.info(f"Generated {len(waveform_data)} samples ({total_size_kb:.1f} KB)")

        # Create a data structure compatible with the visualizer's API
        waveform_payload = {
            "levels": [waveform_data],
            "rates": [target_rate],
            "zoom_thresholds": [1.0],  # A single threshold is sufficient
        }
        
        # Don't keep a reference to waveform_data here - it's now in the payload dict

        def update_visualizer():
            logger.info(f"Updating visualizer with waveform: {duration:.1f}s")
            visualizer.set_waveform(waveform_payload, duration)
            if zoom_control_box:
                zoom_control_box.set_visible(True)

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

        # Clear the global process reference on error
        _current_waveform_process = None

        def clear_visualizer():
            visualizer.set_loading(False)
            visualizer.set_waveform(None, 0)
            # Hide controls when waveform is cleared
            if zoom_control_box:
                zoom_control_box.set_visible(False)
            return False

        GLib.idle_add(clear_visualizer)


def activate_without_waveform(
    file_path, converter_instance, visualizer, file_markers=None, zoom_control_box=None, track_metadata=None
):
    """Activate a file in the visualizer without generating waveform data.
    
    This function is used when waveform generation is disabled but the user still
    needs to interact with the file (e.g., for cutting segments). It retrieves
    the audio duration and sets up the visualizer with no waveform visualization,
    but preserves all functionality like markers and cutting.
    
    Args:
        file_path: Path to the audio file (may be virtual path for tracks)
        converter_instance: AudioConverter instance for accessing ffmpeg_path
        visualizer: Visualizer widget to update
        file_markers: Dictionary of file markers (optional)
        zoom_control_box: Zoom control box widget (optional)
        track_metadata: Dictionary of track metadata for video files (optional)
    """
    try:
        # Check if this is a track extraction
        actual_file_path = file_path
        
        if '::' in file_path and track_metadata and file_path in track_metadata:
            # Extract actual file path
            metadata = track_metadata[file_path]
            actual_file_path = metadata['source_video']
            logger.info(f"Activating track without waveform from {actual_file_path}")

        if not os.path.exists(actual_file_path):
            logger.error(f"File not found: {actual_file_path}")

            def clear_on_error():
                visualizer.set_waveform(None, 0)
                if zoom_control_box:
                    zoom_control_box.set_visible(False)
                return False

            GLib.idle_add(clear_on_error)
            return

        logger.info(f"Activating file without waveform: {actual_file_path}")

        # Get FFmpeg path
        ffprobe_path = getattr(converter_instance, "ffprobe_path", "ffprobe")

        # Use ffprobe to get duration
        try:
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
            result = subprocess.run(
                probe_cmd, capture_output=True, text=True, timeout=5
            )
            duration = float(result.stdout.strip())
            logger.info(f"🔍 WAVEFORM (no-viz): ffprobe returned duration={duration:.6f}s for {os.path.basename(actual_file_path)}")
            logger.info(f"Got duration: {duration:.2f}s")
        except Exception as e:
            logger.error(f"Failed to get duration: {e}")

            def clear_on_error():
                visualizer.set_waveform(None, 0)
                if zoom_control_box:
                    zoom_control_box.set_visible(False)
                return False

            GLib.idle_add(clear_on_error)
            return

        # Update visualizer with no waveform data but with correct duration
        def update_visualizer():
            logger.info(f"Activating visualizer without waveform: {duration:.1f}s")
            visualizer.set_waveform(None, duration)
            if zoom_control_box:
                zoom_control_box.set_visible(True)

            # Restore markers if they exist
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

        def clear_visualizer():
            visualizer.set_waveform(None, 0)
            if zoom_control_box:
                zoom_control_box.set_visible(False)
            return False

        GLib.idle_add(clear_visualizer)
