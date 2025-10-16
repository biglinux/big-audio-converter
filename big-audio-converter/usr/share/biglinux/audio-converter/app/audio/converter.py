# app/audio/converter.py

"""
Audio converter module for handling audio conversion with ffmpeg.
"""

import os
import re
import subprocess
import logging
import tempfile
import gettext
from .segment_processor import SegmentProcessor  # Import the segment processor

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioConverter:
    """
    Audio converter using ffmpeg for conversion functionality.
    """

    def __init__(self, arnndn_model_path=None):
        """Initialize the audio converter."""
        self.ffmpeg_path = self._find_ffmpeg()
        if not self.ffmpeg_path:
            logger.error("ffmpeg not found! Audio conversion will not work.")

        self.arnndn_model_path = arnndn_model_path
        if self.arnndn_model_path and not os.path.exists(self.arnndn_model_path):
            logger.warning(
                f"Provided ARNNDN model path does not exist: {self.arnndn_model_path}"
            )
            self.arnndn_model_path = None

        # Conversion properties
        self.cancel_flag = False
        self.current_process = None

    def _find_ffmpeg(self):
        """Find the ffmpeg executable in the PATH."""
        try:
            # Try the ffmpeg command
            result = subprocess.run(
                ["ffmpeg", "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            if result.returncode == 0:
                return "ffmpeg"
        except FileNotFoundError:
            pass

        # Try common installation locations
        common_paths = [
            "/usr/lib/jellyfin-ffmpeg/ffmpeg",
            "/opt/local/bin/ffmpeg",
        ]

        for path in common_paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path

        return None

    def convert_all_files(self, files, settings, progress_callback, finish_callback):
        """Convert a list of files with the given settings."""
        if not self.ffmpeg_path:
            try:
                from gi.repository import GLib

                GLib.idle_add(
                    finish_callback, False, "ffmpeg not found. Please install FFmpeg."
                )
            except ImportError:
                finish_callback(False, "ffmpeg not found. Please install FFmpeg.")
            return

        try:
            total_files = len(files)
            self.cancel_flag = False
            successful_files = []

            logger.info(f"Starting conversion of {total_files} files")
            # Log cut settings if available
            if settings.get("cut_enabled") and "file_markers" in settings:
                file_markers = settings.get("file_markers", {})
                for file_path in files:
                    if file_path in file_markers:
                        logger.info(
                            f"File {file_path} has {len(file_markers[file_path])} cut segments"
                        )
                    else:
                        logger.info(f"File {file_path} has no cut segments")

            for i, file_path in enumerate(files):
                if self.cancel_flag:
                    # If canceled, report partial success with successfully converted files
                    try:
                        from gi.repository import GLib

                        if successful_files:
                            GLib.idle_add(
                                finish_callback,
                                True,
                                "Conversion partially completed.",
                                successful_files,
                            )
                        else:
                            GLib.idle_add(
                                finish_callback, False, "Conversion canceled."
                            )
                    except ImportError:
                        if successful_files:
                            finish_callback(
                                True,
                                "Conversion partially completed.",
                                successful_files,
                            )
                        else:
                            finish_callback(False, "Conversion canceled.")
                    return

                # Clone settings for each file to prevent interference
                file_settings = settings.copy()

                # Log which file we're processing
                logger.info(
                    f"Processing file {i + 1} of {total_files}: {os.path.basename(file_path)}"
                )

                # Generate output path with proper handling for special characters
                output_format = file_settings["format"]
                output_path = self._get_output_path(file_path, output_format)

                # Process this file with progress updates
                success = self.convert_file(
                    file_path,
                    output_path,
                    file_settings,
                    lambda progress: progress_callback(i, file_path, progress),
                )

                if success:
                    # Track successful conversion
                    successful_files.append(file_path)
                    logger.info(
                        f"Successfully converted file {i + 1}: {os.path.basename(file_path)}"
                    )
                elif not self.cancel_flag:
                    # Report failure for this file but continue with others
                    logger.error(
                        f"Failed to convert file {i + 1}: {os.path.basename(file_path)}"
                    )

            # All files processed, report success with list of converted files
            try:
                from gi.repository import GLib

                if successful_files:
                    GLib.idle_add(
                        finish_callback,
                        True,
                        f"Successfully converted {len(successful_files)} of {total_files} files.",
                        successful_files,
                    )
                else:
                    GLib.idle_add(
                        finish_callback, False, "No files were successfully converted."
                    )
            except ImportError:
                if successful_files:
                    finish_callback(
                        True,
                        f"Successfully converted {len(successful_files)} of {total_files} files.",
                        successful_files,
                    )
                else:
                    finish_callback(False, "No files were successfully converted.")

        except Exception as e:
            logger.exception(f"Error during conversion: {str(e)}")
            try:
                from gi.repository import GLib

                GLib.idle_add(finish_callback, False, f"Conversion error: {str(e)}")
            except ImportError:
                finish_callback(False, f"Conversion error: {str(e)}")

    def convert_file(self, input_path, output_path, settings, progress_callback=None):
        """Convert a single file with the given settings.

        Supports extracting specific audio tracks from video files using track metadata.
        """
        temp_dir = None
        try:
            # Check if this is a virtual track path (format: video_path::track1.ext)
            track_metadata = None
            actual_input_path = input_path

            if "::" in input_path:
                # This is a track extraction request
                logger.info(f"Detected track extraction request: {input_path}")
                if (
                    "track_metadata" in settings
                    and input_path in settings["track_metadata"]
                ):
                    track_metadata = settings["track_metadata"][input_path]
                    actual_input_path = track_metadata["source_video"]
                    logger.info(
                        f"Extracting track {track_metadata['track_index']} from {actual_input_path}"
                    )
                else:
                    logger.error(f"No track metadata found for {input_path}")
                    return False

            # Verify input file exists (use actual file, not virtual path)
            if not os.path.exists(actual_input_path):
                logger.error(f"Input file does not exist: {actual_input_path}")
                return False

            # Handle file-specific cut segments from the file_markers dictionary
            file_has_segments = False
            if settings.get("cut_enabled") and "file_markers" in settings:
                file_markers = settings.get("file_markers", {})
                if input_path in file_markers and file_markers[input_path]:
                    settings["cut_segments"] = file_markers[input_path]
                    file_has_segments = True
                else:
                    settings["cut_segments"] = []
                    file_has_segments = False
            else:
                file_has_segments = False
                settings["cut_segments"] = []

            # Log FFmpeg version for diagnostics
            self._log_ffmpeg_version()

            output_dir = os.path.dirname(output_path)
            if output_dir and not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)

            # Build ffmpeg command
            cmd = [self.ffmpeg_path, "-vn", "-sn", "-y", "-i", actual_input_path]

            if track_metadata:
                track_index = track_metadata["track_index"]
                cmd.extend(["-map", f"0:{track_index}"])
                logger.info(f"Mapping audio stream 0:{track_index}")

            # Handle copy mode
            if settings["format"] == "copy":
                cmd.extend(["-c:a", "copy"])

                # When copying from video files, force output format based on codec
                # CRITICAL: -f must be added AFTER -c:a copy, but BEFORE output path
                if track_metadata:
                    _, output_ext = os.path.splitext(output_path)
                    output_ext_lower = output_ext[1:].lower() if output_ext else ""

                    format_map = {
                        "eac3": "eac3",
                        "ac3": "ac3",
                        "dts": "dts",
                        "flac": "flac",
                        "aac": "adts",
                        "mp3": "mp3",
                        "opus": "opus",
                        "ogg": "ogg",
                        "m4a": "ipod",
                        "wma": "asf",
                    }

                    if output_ext_lower in format_map:
                        cmd.extend(["-f", format_map[output_ext_lower]])
                        logger.info(
                            f"Forcing {format_map[output_ext_lower]} format for video track extraction"
                        )

                audio_filters = []
            else:
                audio_filters = []
                if settings.get("volume", 1.0) != 1.0:
                    audio_filters.append(f"volume={settings['volume']}")
                if settings.get("speed", 1.0) != 1.0:
                    audio_filters.append(f"atempo={settings['speed']}")
                if settings.get("noise_reduction", False) and self.arnndn_model_path:
                    audio_filters.append(f"arnndn=m='{self.arnndn_model_path}'")
                if settings.get("normalize", False):
                    audio_filters.append("loudnorm=I=-16:LRA=11:TP=-1.5")
                if audio_filters:
                    cmd.extend(["-af", ",".join(audio_filters)])
                if settings["format"] == "aac":
                    cmd.extend(["-f", "adts", "-c:a", "aac", "-strict", "-2"])
                else:
                    cmd.extend(["-f", settings["format"]])
                if settings.get("bitrate") and settings["format"] in [
                    "mp3",
                    "aac",
                    "ogg",
                    "opus",
                ]:
                    cmd.extend(["-b:a", settings["bitrate"]])

            self.cancel_requested = False

            # Handle cut functionality
            if file_has_segments:
                segments = settings["cut_segments"]
                if segments:
                    temp_dir = tempfile.mkdtemp(prefix="audioconv_")
                    segment_processor = SegmentProcessor(self.ffmpeg_path)

                    # Determine the actual output format for segment processing
                    # In copy mode, use the OUTPUT file extension (not input)
                    segment_output_format = settings["format"]
                    segment_codec_params = None

                    if segment_output_format == "copy":
                        _, output_ext = os.path.splitext(output_path)
                        segment_output_format = (
                            output_ext[1:].lower() if output_ext else "mp3"
                        )
                    else:
                        # Build codec parameters for encoding (not copy mode)
                        segment_codec_params = []
                        if settings["format"] == "aac":
                            segment_codec_params.extend([
                                "-f",
                                "adts",
                                "-c:a",
                                "aac",
                                "-strict",
                                "-2",
                            ])
                        else:
                            segment_codec_params.extend(["-f", settings["format"]])

                        # Add bitrate if specified for supported formats
                        if settings.get("bitrate") and settings["format"] in [
                            "mp3",
                            "aac",
                            "ogg",
                            "opus",
                        ]:
                            segment_codec_params.extend(["-b:a", settings["bitrate"]])

                    processed_output = segment_processor.process_segments(
                        actual_input_path,
                        segments,
                        segment_output_format,
                        temp_dir,
                        ",".join(audio_filters) if audio_filters else None,
                        track_metadata.get("track_index") if track_metadata else None,
                        output_path,  # Pass final output path for optimization
                        segment_codec_params,  # Pass codec parameters for encoding
                    )
                    if processed_output and os.path.exists(processed_output):
                        import shutil

                        # Check if output is already at final destination (optimization path)
                        if os.path.abspath(processed_output) == os.path.abspath(
                            output_path
                        ):
                            logger.info(
                                f"Conversion with segments successful (direct): {input_path} -> {output_path}"
                            )
                            # Still clean up temp directory if it exists
                            if temp_dir and os.path.exists(temp_dir):
                                try:
                                    shutil.rmtree(temp_dir)
                                    temp_dir = None
                                    logger.debug("Temporary directory cleaned up")
                                except Exception as cleanup_err:
                                    logger.warning(
                                        f"Failed to clean up temp directory: {cleanup_err}"
                                    )
                            return True

                        # Otherwise copy from temp to final destination
                        try:
                            shutil.copy2(processed_output, output_path)
                            logger.info(
                                f"Conversion with segments successful: {input_path} -> {output_path}"
                            )
                        finally:
                            # Clean up temp directory immediately after copying
                            if temp_dir and os.path.exists(temp_dir):
                                try:
                                    shutil.rmtree(temp_dir)
                                    temp_dir = None  # Mark as cleaned
                                    logger.debug(
                                        "Temporary directory cleaned up after segment processing"
                                    )
                                except Exception as cleanup_err:
                                    logger.warning(
                                        f"Failed to clean up temp directory: {cleanup_err}"
                                    )
                        return True
                    else:
                        logger.error("Segment processing failed.")
                        # Clean up temp directory on failure too
                        if temp_dir and os.path.exists(temp_dir):
                            try:
                                import shutil

                                shutil.rmtree(temp_dir)
                                temp_dir = None
                            except Exception as cleanup_err:
                                logger.warning(
                                    f"Failed to clean up temp directory: {cleanup_err}"
                                )
                        return False
                else:
                    # Fall through to normal conversion if no valid segments
                    pass

            # Normal conversion (no segments)
            cmd.append(output_path)
            print("\n=== FFMPEG COMMAND ===")
            print(f"{' '.join(cmd)}")
            print("======================\n")
            logger.info("=== FFMPEG COMMAND ===")
            logger.info(f"{' '.join(cmd)}")
            logger.info("======================")
            logger.debug(f"FFmpeg command: {' '.join(cmd)}")

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                text=True,
            )
            self.current_process = process

            duration = self._get_duration(actual_input_path) or 0
            if progress_callback and duration > 0:
                for line in process.stderr:
                    if self.cancel_flag:
                        process.terminate()
                        return False
                    time_match = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
                    if time_match:
                        hours, minutes, seconds = map(float, time_match.groups())
                        current_time = hours * 3600 + minutes * 60 + seconds
                        progress = min(current_time / duration, 1.0)
                        progress_callback(progress)

            process.wait()
            self.current_process = None

            if process.returncode != 0 and not self.cancel_flag:
                logger.error(
                    f"FFmpeg error (code {process.returncode}):\n{process.stderr.read()}"
                )
                return False

            if not os.path.exists(output_path):
                logger.error("FFmpeg did not create output file")
                return False

            logger.info(f"Conversion successful: {input_path} -> {output_path}")
            return not self.cancel_flag

        except Exception as e:
            logger.exception(f"Error during conversion: {str(e)}")
            return False
        finally:
            self.current_process = None
            if temp_dir and os.path.exists(temp_dir):
                import shutil

                try:
                    shutil.rmtree(temp_dir)
                    logger.debug("Temporary directory cleaned up")
                except Exception as e:
                    logger.error(f"Failed to clean up temp directory: {e}")

    def _log_ffmpeg_version(self):
        """Log FFmpeg version for diagnostics."""
        if not self.ffmpeg_path:
            return

        try:
            result = subprocess.run(
                [self.ffmpeg_path, "-version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                version_line = result.stdout.split("\n")[0]
                logger.debug(f"Using FFmpeg: {version_line}")
        except Exception as e:
            logger.warning(f"Could not determine FFmpeg version: {str(e)}")

    def _get_duration(self, file_path):
        """Get the duration of an audio file in seconds."""
        if not self.ffmpeg_path:
            return 0

        # Use ffprobe to get the duration
        ffprobe_path = self.ffmpeg_path.replace("ffmpeg", "ffprobe")
        if not os.path.exists(ffprobe_path):
            ffprobe_path = "ffprobe"  # Try using the command directly

        try:
            cmd = [
                ffprobe_path,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                file_path,
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout:
                return float(result.stdout.strip())
        except Exception as e:
            logger.error(f"Error getting duration: {str(e)}")

        return 0

    def _get_output_path(self, input_path, output_format):
        """Generate the output path based on input path and format."""
        # Handle virtual track paths (format: video_path::trackN.ext)
        if "::" in input_path:
            # Extract the video path and track filename
            video_path, track_filename = input_path.split("::", 1)
            # Use the video directory as the output directory
            input_dir = os.path.dirname(video_path)
            # Get video filename without extension
            video_basename = os.path.splitext(os.path.basename(video_path))[0]
            # Get track number/name from track filename
            track_base, track_ext = os.path.splitext(track_filename)
            # Combine video name with track name
            filename_base = f"{video_basename}-{track_base}"
            input_extension = track_ext[1:].lower() if track_ext else ""
        else:
            # Use os.path for reliable path manipulation with special characters
            input_dir = os.path.dirname(input_path)
            input_filename = os.path.basename(input_path)

            # Get filename parts safely
            filename_base, ext = os.path.splitext(input_filename)
            input_extension = ext[1:].lower() if ext else ""

        # Create output filename with new extension
        if output_format == "copy":
            output_filename = f"{filename_base}.{input_extension}"
        else:
            output_filename = f"{filename_base}.{output_format}"

        # If the input and output formats are the same, add '-converted' suffix
        if output_format.lower() == input_extension and output_format != "copy":
            output_filename = f"{filename_base}-converted.{output_format}"

        output_path = os.path.join(input_dir, output_filename)

        # CRITICAL: Prevent overwriting input file - if output == input, always add suffix
        if "::" not in input_path and output_path == input_path:
            if output_format == "copy":
                output_filename = f"{filename_base}-copy.{input_extension}"
            else:
                output_filename = f"{filename_base}-converted.{output_format}"
            output_path = os.path.join(input_dir, output_filename)

        # If the file already exists, append a sequence number
        counter = 1
        while os.path.exists(output_path):
            if output_format == "copy":
                output_filename = f"{filename_base}-copy-{counter}.{input_extension}"
            else:
                output_filename = f"{filename_base}-converted-{counter}.{output_format}"
            output_path = os.path.join(input_dir, output_filename)
            counter += 1

            # Safety check to avoid infinite loops
            if counter > 100:
                break

        return output_path

    def cancel_conversion(self):
        """Cancel the current conversion process."""
        self.cancel_flag = True
        if self.current_process:
            try:
                self.current_process.terminate()
            except Exception as e:
                logger.error(f"Error terminating process: {str(e)}")

    def get_file_metadata(self, file_path):
        """Extract file metadata like size, duration, format."""
        info = {}

        # Get file size
        try:
            size_bytes = os.path.getsize(file_path)
            if size_bytes < 1024 * 1024:  # Less than 1MB
                info["size"] = f"{size_bytes / 1024:.1f} KB"
            else:
                info["size"] = f"{size_bytes / (1024 * 1024):.1f} MB"
        except Exception as e:
            logger.warning(f"Could not get file size: {e}")

        # Get file format/extension
        try:
            ext = os.path.splitext(file_path)[1]
            if ext.startswith("."):
                ext = ext[1:]
            info["format"] = ext.upper()
        except Exception as e:
            logger.warning(f"Could not get file extension: {e}")

        # Get audio duration and bitrate using ffprobe if available
        try:
            if self.ffmpeg_path:
                ffprobe_path = self.ffmpeg_path.replace("ffmpeg", "ffprobe")

                if not os.path.exists(ffprobe_path):
                    ffprobe_path = "ffprobe"  # Try using command directly

                # Get duration
                cmd_duration = [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ]

                result = subprocess.run(
                    cmd_duration, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    duration_secs = float(result.stdout.strip())
                    # Format duration as MM:SS
                    minutes = int(duration_secs // 60)
                    seconds = int(duration_secs % 60)
                    info["duration"] = f"{minutes}:{seconds:02d}"

                # Get bitrate
                cmd_bitrate = [
                    ffprobe_path,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=bit_rate",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    file_path,
                ]

                result = subprocess.run(
                    cmd_bitrate, capture_output=True, text=True, timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        # Convert bits/s to kbps
                        bitrate = int(result.stdout.strip()) // 1000
                        info["bitrate"] = f"{bitrate} kbps"
                    except ValueError:
                        pass
        except Exception as e:
            logger.warning(f"Could not get file audio metadata: {e}")

        return info

    def cleanup(self):
        """Clean up any resources."""
        self.cancel_conversion()  # Make sure any ongoing conversions are stopped
