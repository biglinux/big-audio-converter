"""
Audio converter module for handling audio conversion and processing.
"""

import os
import subprocess
import logging
import threading
import time
import re
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


class AudioConverter:
    """Audio conversion functionality using ffmpeg."""

    def __init__(self):
        """Initialize the audio converter."""
        self.ffmpeg_path = self._find_ffmpeg()
        if not self.ffmpeg_path:
            logger.error("ffmpeg not found! Audio conversion will not work.")

        self.current_file = None
        self.temp_dir = None
        self.cut_start = None
        self.cut_end = None
        self.cancel_requested = False

    def _find_ffmpeg(self):
        """Find the ffmpeg executable in the PATH."""
        # Check if ffmpeg is available
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
            "/usr/bin/ffmpeg",
            "/usr/local/bin/ffmpeg",
            "/opt/local/bin/ffmpeg",
            "C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe",
            "C:\\ffmpeg\\bin\\ffmpeg.exe",
        ]

        for path in common_paths:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path

        return None

    def _get_audio_duration(self, file_path):
        """Get the duration of an audio file in seconds."""
        if not self.ffmpeg_path:
            return 0

        try:
            cmd = [
                self.ffmpeg_path,
                "-i",
                file_path,
                "-hide_banner",
                "-show_entries",
                "format=duration",
                "-v",
                "quiet",
                "-of",
                "csv=p=0",
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode == 0 and result.stdout:
                try:
                    return float(result.stdout.strip())
                except ValueError:
                    pass
        except Exception as e:
            logger.error(f"Error getting duration: {str(e)}")

        return 0

    def _monitor_progress(self, process, total_duration, progress_callback):
        """Monitor ffmpeg progress by parsing its output."""
        if not progress_callback:
            return

        pattern = re.compile(r"time=(\d+):(\d+):(\d+\.\d+)")

        while process.poll() is None and not self.cancel_requested:
            output = process.stderr.readline()
            if not output:
                time.sleep(0.1)
                continue

            match = pattern.search(output)
            if match:
                h, m, s = map(float, match.groups())
                current_time = h * 3600 + m * 60 + s
                progress = (
                    min(current_time / total_duration, 1.0) if total_duration > 0 else 0
                )
                progress_callback(progress)

        # Ensure we report 100% when done (if not cancelled)
        if not self.cancel_requested and process.poll() == 0:
            progress_callback(1.0)

    def convert_file(self, input_file, output_format, settings, progress_callback=None):
        """
        Convert a single audio file with the specified settings.

        Args:
            input_file (str): Path to the input file
            output_format (str): Output format (e.g., mp3, ogg, flac)
            settings (dict): Conversion settings
            progress_callback (function, optional): Callback for conversion progress

        Returns:
            tuple: (success, output_file_path or error_message)
        """
        if not self.ffmpeg_path:
            return False, "ffmpeg not found. Please install ffmpeg and try again."

        if not os.path.isfile(input_file):
            return False, f"Input file not found: {input_file}"

        # Create output filename
        input_path = Path(input_file)
        output_file = input_path.with_suffix(f".{output_format}")

        # If output file would overwrite input, add suffix
        if output_file == input_path:
            output_file = input_path.with_name(
                f"{input_path.stem}_converted{output_file.suffix}"
            )

        # Make sure the output directory exists
        os.makedirs(os.path.dirname(output_file), exist_ok=True)

        # Build ffmpeg command
        cmd = [self.ffmpeg_path, "-y"]  # Overwrite output file if exists

        # Add input file
        cmd.extend(["-i", str(input_file)])

        # Add cut options if specified
        if (
            self.cut_start is not None
            and self.cut_end is not None
            and self.cut_end > self.cut_start
        ):
            duration = self.cut_end - self.cut_start
            cmd.extend(["-ss", str(self.cut_start), "-t", str(duration)])

        # Add format-specific options
        if output_format == "mp3":
            cmd.extend(["-c:a", "libmp3lame"])
            if "bitrate" in settings:
                cmd.extend(["-b:a", settings["bitrate"]])
        elif output_format == "ogg":
            cmd.extend(["-c:a", "libvorbis"])
            if "bitrate" in settings:
                cmd.extend(["-b:a", settings["bitrate"]])
        elif output_format == "opus":
            cmd.extend(["-c:a", "libopus"])
            if "bitrate" in settings:
                cmd.extend(["-b:a", settings["bitrate"]])
        elif output_format == "flac":
            cmd.extend(["-c:a", "flac"])
        elif output_format == "aac":
            cmd.extend(["-c:a", "aac"])
            if "bitrate" in settings:
                cmd.extend(["-b:a", settings["bitrate"]])

        # Add audio filters
        audio_filters = []

        # Add speed adjustment if specified
        if "speed" in settings and settings["speed"] != 1.0:
            speed = float(settings["speed"])
            audio_filters.append(f"atempo={speed}")

        # Add volume adjustment if specified
        if "volume" in settings and settings["volume"] != 1.0:
            volume = float(settings["volume"])
            audio_filters.append(f"volume={volume}")

        # Add noise reduction if enabled
        if settings.get("noise_reduction", False):
            audio_filters.append("arnndn")

        # Add normalization if enabled
        if settings.get("normalize", False):
            audio_filters.append("loudnorm")

        # Add fade in/out if specified
        fade_in = settings.get("fade_in", 0)
        fade_out = settings.get("fade_out", 0)
        if fade_in > 0:
            audio_filters.append(f"afade=t=in:st=0:d={fade_in}")
        if fade_out > 0:
            # For fade out, we need the duration
            if self.cut_start is not None and self.cut_end is not None:
                duration = self.cut_end - self.cut_start
            else:
                # Get duration from input file
                duration = self._get_audio_duration(input_file)

            if duration > 0:
                audio_filters.append(
                    f"afade=t=out:st={duration - fade_out}:d={fade_out}"
                )

        # Apply audio filters if any
        if audio_filters:
            cmd.extend(["-filter:a", ",".join(audio_filters)])

        # Output file
        cmd.append(str(output_file))

        logger.debug(f"Running conversion command: {' '.join(cmd)}")

        try:
            self.cancel_requested = False

            # Start the ffmpeg process
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                text=True,
            )

            # Get duration for progress calculation
            duration = self._get_audio_duration(input_file)

            # Start progress monitoring in a separate thread
            if progress_callback and duration > 0:
                threading.Thread(
                    target=self._monitor_progress,
                    args=(process, duration, progress_callback),
                    daemon=True,
                ).start()

            # Wait for process to complete
            stdout, stderr = process.communicate()

            # Check if cancelled
            if self.cancel_requested:
                if os.path.exists(output_file):
                    os.remove(output_file)
                return False, "Conversion cancelled"

            # Check if conversion was successful
            if process.returncode != 0:
                logger.error(f"Conversion failed: {stderr}")
                return (
                    False,
                    f"Conversion failed: {stderr.splitlines()[-1] if stderr else 'Unknown error'}",
                )

            logger.info(f"Successfully converted {input_file} to {output_file}")
            return True, str(output_file)

        except Exception as e:
            logger.exception(f"Error during conversion: {str(e)}")
            return False, f"Conversion error: {str(e)}"

    def convert_all_files(
        self, files, settings, progress_callback=None, complete_callback=None
    ):
        """
        Convert multiple files sequentially.

        Args:
            files (list): List of file paths to convert
            settings (dict): Conversion settings
            progress_callback (function): Called with (file_index, file_path, progress)
            complete_callback (function): Called when all conversions are done
        """
        if not files:
            if complete_callback:
                complete_callback(True)
            return

        output_format = settings.get("format", "mp3")
        success_count = 0
        errors = []

        try:
            for i, file_path in enumerate(files):
                if self.cancel_requested:
                    break

                # File-specific progress callback
                def file_progress(progress):
                    if progress_callback:
                        progress_callback(i, file_path, progress)

                # Convert the file
                success, result = self.convert_file(
                    file_path, output_format, settings, file_progress
                )

                if success:
                    success_count += 1
                else:
                    errors.append(f"{os.path.basename(file_path)}: {result}")

            # Call the completion callback
            if complete_callback:
                if not errors:
                    complete_callback(True)
                else:
                    error_message = (
                        f"Completed with errors ({success_count}/{len(files)} successful):\n"
                        + "\n".join(errors)
                    )
                    complete_callback(False, error_message)

        except Exception as e:
            logger.exception(f"Error in batch conversion: {str(e)}")
            if complete_callback:
                complete_callback(False, f"Batch conversion error: {str(e)}")

    def cancel_conversion(self):
        """Cancel the current conversion process."""
        self.cancel_requested = True
        logger.info("Conversion cancellation requested")

    def cleanup(self):
        """Clean up temporary files."""
        if self.temp_dir and os.path.exists(self.temp_dir):
            shutil.rmtree(self.temp_dir, ignore_errors=True)
            self.temp_dir = None
