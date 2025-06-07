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
                    if successful_files:
                        finish_callback(
                            True, "Conversion partially completed.", successful_files
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
            finish_callback(False, f"Conversion error: {str(e)}")

    def convert_file(self, input_path, output_path, settings, progress_callback=None):
        """Convert a single file with the given settings."""
        try:
            # Verify input file exists
            if not os.path.exists(input_path):
                logger.error(f"Input file does not exist: {input_path}")
                return False

            # Handle file-specific cut segments from the file_markers dictionary
            file_has_segments = False
            if settings.get("cut_enabled") and "file_markers" in settings:
                file_markers = settings.get("file_markers", {})

                # Check if this specific file has markers
                if input_path in file_markers and file_markers[input_path]:
                    # Use this file's specific markers
                    segment_count = len(file_markers[input_path])
                    logger.info(
                        f"File {input_path} has {segment_count} segments for cutting"
                    )
                    settings["cut_segments"] = file_markers[input_path]
                    file_has_segments = segment_count > 0
                else:
                    logger.info(f"No cut segments defined for {input_path}")
                    settings["cut_segments"] = []
                    file_has_segments = False
            else:
                logger.info(f"Cutting is disabled for {input_path}")
                file_has_segments = False
                settings["cut_segments"] = []

            # Continue with normal conversion using the file-specific settings
            if not self.ffmpeg_path:
                return False

            if not os.path.isfile(input_path):
                logger.error(f"Input file not found: {input_path}")
                return False

            # Create a temporary directory for conversion
            temp_dir = None

            try:
                # Log FFmpeg version for diagnostics
                self._log_ffmpeg_version()

                # Always use strings for paths with FFmpeg to avoid issues with special chars
                input_path_str = str(input_path)

                # Use the provided output path or generate a new one
                if output_path is None:
                    output_format = settings.get("format", "mp3")
                    output_path = self._get_output_path(input_path, output_format)

                output_path_str = str(output_path)
                output_format = settings.get(
                    "format", os.path.splitext(output_path_str)[1][1:] or "mp3"
                )

                # Check input file size for diagnostics
                try:
                    input_size = os.path.getsize(input_path_str)
                    logger.debug(f"Input file size: {input_size} bytes")
                    if input_size == 0:
                        logger.error("Input file is empty")
                        return False
                except Exception as e:
                    logger.warning(f"Could not check input file size: {str(e)}")

                # Create a temporary directory with simple filenames to avoid special character issues
                import shutil

                temp_dir = tempfile.mkdtemp(prefix="audioconv_")
                temp_input = os.path.join(
                    temp_dir, f"input.{os.path.splitext(input_path)[1][1:] or 'mp3'}"
                )
                temp_output = os.path.join(temp_dir, f"output.{output_format}")

                # Copy input file to temp directory
                logger.debug(f"Copying input file to temporary location: {temp_input}")
                try:
                    shutil.copy2(input_path_str, temp_input)
                    # Verify the copy was successful
                    if not os.path.exists(temp_input):
                        logger.error("Failed to copy input file to temp directory")
                        return False
                    temp_input_size = os.path.getsize(temp_input)
                    logger.debug(f"Temp input file size: {temp_input_size} bytes")
                except Exception as e:
                    logger.error(
                        f"Error copying input file to temp directory: {str(e)}"
                    )
                    return False

                # Make sure the output directory exists
                output_dir = os.path.dirname(output_path_str)
                if output_dir and not os.path.exists(output_dir):
                    os.makedirs(output_dir, exist_ok=True)

                # Build ffmpeg command with simple temp paths
                cmd = [self.ffmpeg_path, "-y"]  # Overwrite output file if exists

                # Add input file (now the temp file with simple name)
                cmd.extend(["-i", temp_input])

                # Set up audio filters
                audio_filters = []

                # Volume adjustment
                if settings.get("volume", 1.0) != 1.0:
                    audio_filters.append(f"volume={settings['volume']}")

                # Speed/tempo adjustment
                if settings.get("speed", 1.0) != 1.0:
                    audio_filters.append(f"atempo={settings['speed']}")

                # Noise reduction if enabled
                if settings.get("noise_reduction", False) and self.arnndn_model_path:
                    # Use single quotes for the model path in ffmpeg filter
                    audio_filters.append(f"arnndn=m='{self.arnndn_model_path}'")
                elif (
                    settings.get("noise_reduction", False)
                    and not self.arnndn_model_path
                ):
                    logger.warning(
                        "Noise reduction enabled, but ARNNDN model not found. Skipping FFmpeg noise reduction."
                    )

                # Normalization
                if settings.get("normalize", False):
                    audio_filters.append("loudnorm=I=-16:LRA=11:TP=-1.5")

                # Apply audio filters if any
                if audio_filters:
                    cmd.extend(["-af", ",".join(audio_filters)])

                # Add output format options
                if output_format == "aac":
                    # Use ADTS muxer for raw AAC output
                    cmd.extend(["-f", "adts"])
                else:
                    cmd.extend(["-f", output_format])

                # Set audio codec for AAC explicitly
                if output_format == "aac":
                    cmd.extend(["-c:a", "aac"])
                    # Optionally, add strict -2 for native encoder compatibility
                    cmd.extend(["-strict", "-2"])

                # Set bitrate if specified and applicable
                if settings.get("bitrate") and output_format in [
                    "mp3",
                    "aac",
                    "ogg",
                    "opus",
                ]:
                    cmd.extend(["-b:a", settings["bitrate"]])

                self.cancel_requested = False

                # Handle cut functionality
                if settings.get("cut_enabled") and file_has_segments:
                    segments = settings["cut_segments"]

                    segment_count = len(segments)
                    logger.info(
                        f"Processing {segment_count} cut segments for {os.path.basename(input_path)}"
                    )

                    # Ensure we only use valid segments
                    valid_segments = [
                        seg
                        for seg in segments
                        if "start" in seg
                        and "stop" in seg
                        and seg["start"] is not None
                        and seg["stop"] is not None
                    ]

                    if valid_segments:
                        logger.info(
                            f"Found {len(valid_segments)} valid segments to process"
                        )
                        for i, segment in enumerate(valid_segments):
                            if "start_str" in segment and "stop_str" in segment:
                                logger.debug(
                                    f"Segment {i + 1}: {segment['start_str']} to {segment['stop_str']}"
                                )

                        # Use the segment processor to handle all segments
                        segment_processor = SegmentProcessor(self.ffmpeg_path)

                        # Prepare audio filters string
                        audio_filter_string = None
                        if audio_filters:
                            audio_filter_string = ",".join(audio_filters)

                        # Process all segments and get resulting file
                        processed_output = segment_processor.process_segments(
                            temp_input,
                            valid_segments,
                            output_format,
                            temp_dir,
                            audio_filter_string,
                        )

                        # If processing was successful, update temp_output path
                        if processed_output and os.path.exists(processed_output):
                            logger.info(
                                f"Successfully processed {len(valid_segments)} segments"
                            )
                            temp_output = processed_output

                            # We've already done all the processing, no need for further FFmpeg commands
                            # Skip to copying the file to the final destination
                            if os.path.exists(temp_output) and not self.cancel_flag:
                                # Copy the processed output to the final destination
                                try:
                                    shutil.copy2(temp_output, output_path_str)
                                    logger.info(
                                        f"Conversion with segments successful: {input_path_str} -> {output_path_str}"
                                    )
                                    return True
                                except Exception as e:
                                    logger.error(
                                        f"Error copying segmented output file: {e}"
                                    )
                                    return False
                        else:
                            logger.error(
                                "Segment processing failed, no output file produced"
                            )
                            return False
                    else:
                        logger.warning(
                            "No valid segments found, proceeding with normal conversion"
                        )
                        # Continue with normal processing (code below)
                        pass

                # Normal conversion (either no cut enabled or no valid segments)
                cmd.append(temp_output)
                logger.info(
                    f"Running normal conversion (no segments) for {os.path.basename(input_path)}"
                )
                logger.debug(f"FFmpeg command: {' '.join(cmd)}")

                # Start the ffmpeg process with timeout to avoid hanging
                try:
                    # If this is a simple processing (not multi-segment), run it here
                    if not (
                        settings.get("cut_enabled")
                        and settings.get("cut_segments")
                        and len(settings["cut_segments"]) > 1
                    ):
                        process = subprocess.Popen(
                            cmd,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            universal_newlines=True,
                            text=True,
                        )

                        # Get duration of input file for progress calculation
                        duration = self._get_duration(temp_input) or 0
                        logger.debug(f"File duration: {duration} seconds")

                        # Set a timeout for reading process output (5 minutes) to prevent hanging
                        import time

                        start_time = time.time()
                        max_conversion_time = 300  # 5 minutes in seconds

                        # Read stderr output line by line to parse progress
                        if progress_callback and duration > 0:
                            stderr_lines = []
                            while process.poll() is None:
                                # Check for timeout
                                if time.time() - start_time > max_conversion_time:
                                    logger.error(
                                        f"FFmpeg conversion timed out after {max_conversion_time} seconds"
                                    )
                                    process.terminate()
                                    return False

                                # Check for cancellation
                                if self.cancel_flag:
                                    process.terminate()
                                    return False

                                # Try to read a line with timeout
                                try:
                                    line = process.stderr.readline()
                                    if not line:
                                        time.sleep(0.1)
                                        continue

                                    # Save for debugging
                                    stderr_lines.append(line)

                                    # Extract time position from ffmpeg output
                                    time_match = re.search(
                                        r"time=(\d+):(\d+):(\d+\.\d+)", line
                                    )
                                    if time_match:
                                        hours, minutes, seconds = map(
                                            float, time_match.groups()
                                        )
                                        current_time = (
                                            hours * 3600 + minutes * 60 + seconds
                                        )
                                        progress = min(current_time / duration, 1.0)
                                        progress_callback(progress)
                                except Exception as e:
                                    logger.warning(
                                        f"Error reading FFmpeg output: {str(e)}"
                                    )

                            # Save full stderr for debugging if needed
                            if stderr_lines:
                                logger.debug(
                                    f"FFmpeg stderr output: {len(stderr_lines)} lines"
                                )
                        else:
                            # If we can't report progress, just wait for completion with timeout
                            try:
                                process.wait(timeout=max_conversion_time)
                            except subprocess.TimeoutExpired:
                                logger.error(
                                    f"FFmpeg conversion timed out after {max_conversion_time} seconds"
                                )
                                process.terminate()
                                return False

                        # Get the return code
                        return_code = process.poll()
                        if return_code is None:
                            # Process is still running, try to terminate
                            logger.warning(
                                "FFmpeg process not properly terminated, forcing termination"
                            )
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except:
                                process.kill()
                            return False

                        if return_code != 0 and not self.cancel_flag:
                            # Read all stderr output for diagnostics
                            try:
                                process.stderr.seek(0)
                            except Exception:
                                pass
                            error_output = ""
                            if process.stderr:
                                try:
                                    error_output = process.stderr.read()
                                except Exception:
                                    pass
                            logger.error(
                                f"FFmpeg error (code {return_code}):\n{error_output}"
                            )
                            return False

                        # Check if output file was created
                        if not os.path.exists(temp_output):
                            logger.error("FFmpeg did not create output file")
                            return False

                        # Check output file size
                        try:
                            output_size = os.path.getsize(temp_output)
                            logger.debug(
                                f"Temporary output file size: {output_size} bytes"
                            )
                            if output_size == 0:
                                logger.error("FFmpeg created empty output file")
                                return False
                        except Exception as e:
                            logger.warning(
                                f"Could not check output file size: {str(e)}"
                            )

                        # If conversion was successful, copy the output file to the final destination
                        if os.path.exists(temp_output) and not self.cancel_flag:
                            # Check again if the output path exists (could have been created by another process)
                            # and generate a new unique path if needed
                            if os.path.exists(output_path_str):
                                logger.debug(
                                    "Output path already exists, finding alternative name"
                                )
                                output_path_str = self._get_safe_output_path(
                                    output_path_str
                                )

                            logger.debug(
                                f"Copying output file to final destination: {output_path_str}"
                            )
                            try:
                                shutil.copy2(temp_output, output_path_str)
                                # Verify the copy was successful
                                if not os.path.exists(output_path_str):
                                    logger.error(
                                        "Failed to copy output file to destination"
                                    )
                                    return False

                                # Verify file size after copy
                                try:
                                    final_size = os.path.getsize(output_path_str)
                                    temp_size = os.path.getsize(temp_output)
                                    if (
                                        final_size == 0 or final_size < temp_size * 0.9
                                    ):  # Allow for some metadata loss
                                        logger.warning(
                                            f"Output file size mismatch: temp={temp_size}, final={final_size}"
                                        )
                                except Exception as e:
                                    logger.warning(
                                        f"Could not verify output file size: {str(e)}"
                                    )

                            except Exception as e:
                                logger.error(
                                    f"Error copying output file to destination: {str(e)}"
                                )
                                return False

                            logger.info(
                                f"Conversion successful: {input_path_str} -> {output_path_str}"
                            )
                            return True

                        return not self.cancel_flag

                    else:
                        # For multi-segment processing, we've already done everything
                        # Copy the concatenated output to the destination
                        if os.path.exists(temp_output) and not self.cancel_flag:
                            # Check again if the output path exists
                            if os.path.exists(output_path_str):
                                logger.debug(
                                    "Output path already exists, finding alternative name"
                                )
                                output_path_str = self._get_safe_output_path(
                                    output_path_str
                                )

                            logger.debug(
                                f"Copying output file to final destination: {output_path_str}"
                            )
                            try:
                                shutil.copy2(temp_output, output_path_str)
                                # Verify the copy was successful
                                if not os.path.exists(output_path_str):
                                    logger.error(
                                        "Failed to copy output file to destination"
                                    )
                                    return False

                                # Log success
                                logger.info(
                                    f"Conversion successful: {input_path_str} -> {output_path_str}"
                                )
                                return True

                            except Exception as e:
                                logger.error(
                                    f"Error copying output file to destination: {str(e)}"
                                )
                                return False

                        # If we don't have an output file, something went wrong
                        if not os.path.exists(temp_output):
                            logger.error(
                                "Multi-segment processing failed to produce output file"
                            )
                            return False

                        return not self.cancel_flag

                except Exception as ffmpeg_error:
                    logger.exception(f"FFmpeg process error: {str(ffmpeg_error)}")
                    return False

            except Exception as e:
                logger.exception(f"Error during conversion: {str(e)}")
                return False
            finally:
                # Clean up temporary directory
                if temp_dir and os.path.exists(temp_dir):
                    try:
                        import shutil

                        shutil.rmtree(temp_dir)
                        logger.debug("Temporary directory cleaned up")
                    except Exception as e:
                        logger.error(f"Failed to clean up temp directory: {e}")

                # Clear current process reference
                self.current_process = None

        except Exception as e:
            logger.error(f"Error during conversion: {str(e)}")
            return False
        finally:
            # Cleanup any temporary resources
            pass

        return True  # Return success if we reach here

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
        # Use os.path for reliable path manipulation with special characters
        input_dir = os.path.dirname(input_path)
        input_filename = os.path.basename(input_path)

        # Get filename parts safely
        filename_base, ext = os.path.splitext(input_filename)
        input_extension = ext[1:].lower() if ext else ""

        # Create output filename with new extension
        output_filename = f"{filename_base}.{output_format}"

        # If the input and output formats are the same, add '-converted' suffix
        if output_format.lower() == input_extension:
            output_filename = f"{filename_base}-converted.{output_format}"

        output_path = os.path.join(input_dir, output_filename)

        # If the file already exists, append a sequence number
        counter = 1
        while os.path.exists(output_path) and output_path != input_path:
            output_filename = f"{filename_base}-{counter}.{output_format}"
            output_path = os.path.join(input_dir, output_filename)
            counter += 1

            # Safety check to avoid infinite loops
            if counter > 100:
                break

        return output_path

    def _get_safe_output_path(self, path):
        """Generate a unique filename by adding a number to avoid overwriting."""
        if not os.path.exists(path):
            return path

        directory = os.path.dirname(path)
        filename = os.path.basename(path)
        name, ext = os.path.splitext(filename)

        # Check if name already ends with -N pattern
        match = re.search(r"-(\d+)$", name)
        if match:
            base = name[: match.start()]
            num = int(match.group(1)) + 1
        else:
            base = name
            num = 1

        # Try new filenames with incrementing numbers
        while True:
            new_name = f"{base}-{num}{ext}"
            new_path = os.path.join(directory, new_name)
            if not os.path.exists(new_path):
                return new_path
            num += 1
            # Safety check
            if num > 999:
                # Just use timestamp as last resort
                import time

                new_name = f"{base}-{int(time.time())}{ext}"
                return os.path.join(directory, new_name)

    def cancel_conversion(self):
        """Cancel the current conversion process."""
        self.cancel_flag = True
        if self.current_process:
            try:
                self.current_process.terminate()
            except Exception as e:
                logger.error(f"Error terminating process: {str(e)}")

    def cleanup(self):
        """Clean up any resources."""
        self.cancel_conversion()  # Make sure any ongoing conversions are stopped
