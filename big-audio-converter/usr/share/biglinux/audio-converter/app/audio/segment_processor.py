"""
Segment processor for handling audio segments during conversion.
"""

import os
import logging
import subprocess
import tempfile

logger = logging.getLogger(__name__)


class SegmentProcessor:
    """
    Helper class for processing audio segments during conversion.
    Ensures all segments are properly cut and combined.
    """

    def __init__(self, ffmpeg_path):
        self.ffmpeg_path = ffmpeg_path

    def process_segments(
        self, input_file, segments, output_format, temp_dir, audio_filters=None
    ):
        """
        Process multiple segments from input file and return path to the processed output.

        Args:
            input_file: Path to the input audio file
            segments: List of segment dictionaries with start and stop times
            output_format: Output audio format (mp3, ogg, etc.)
            temp_dir: Directory for temporary files
            audio_filters: Optional audio filters to apply

        Returns:
            Path to the processed output file or None if processing failed
        """
        if not segments or len(segments) == 0:
            logger.warning("No segments provided for processing")
            return None

        logger.info(f"Processing {len(segments)} segments from {input_file}")

        # Original segment order for debugging
        print(
            f"Original segment order: {[(s.get('segment_index', '?'), s['start_str']) for s in segments]}"
        )

        # Filter out invalid segments
        valid_segments = self._validate_segments(segments)
        if not valid_segments:
            logger.warning("No valid segments found after validation")
            return None

        # CRITICAL FIX: DO NOT sort segments by time - preserve exact order provided

        # Calculate total expected duration for verification
        total_expected_duration = sum(
            seg["stop"] - seg["start"] for seg in valid_segments
        )
        logger.debug(
            f"Expected total duration after concatenation: {total_expected_duration:.2f} seconds"
        )

        # Process each segment separately
        temp_segments = []

        for i, segment in enumerate(valid_segments):
            segment_output = os.path.join(temp_dir, f"segment_{i}.{output_format}")

            if self._extract_segment(
                input_file, segment, segment_output, audio_filters
            ):
                temp_segments.append(segment_output)
                logger.debug(
                    f"Successfully extracted segment {i + 1}: {segment_output}"
                )
            else:
                logger.error(f"Failed to extract segment {i + 1}")

        # If no segments were successfully extracted, return None
        if not temp_segments:
            logger.error("No segments were successfully extracted")
            return None

        # If only one segment, use it directly
        if len(temp_segments) == 1:
            return temp_segments[0]

        # Otherwise concatenate all segments
        output_file = os.path.join(temp_dir, f"combined_output.{output_format}")
        if self._concatenate_segments(temp_segments, output_file):
            return output_file

        return None

    def _validate_segments(self, segments):
        """Validate segments and return only valid ones."""
        valid_segments = []

        # Debugging: Print what we received
        print(f"Validating {len(segments)} segments from converter")

        for segment in segments:
            # Log the segment we're processing
            print(f"Processing segment: {segment}")

            start = segment.get("start")
            stop = segment.get("stop")
            start_str = segment.get("start_str", "")
            stop_str = segment.get("stop_str", "")

            # Skip segments with missing start/stop or invalid values
            if start is None or stop is None:
                print(f"Skipping segment with missing values: {segment}")
                continue

            # Skip segments that are too short (less than 100ms)
            if abs(stop - start) < 0.1:
                print(f"Skipping segment that is too short: {segment}")
                continue

            # Verify we have at least one valid time string
            if not start_str or not stop_str:
                print(f"Missing time strings, using numeric values")
                # Use our numeric values to create strings if needed
                if not start_str:
                    start_str = self._format_time(start)
                if not stop_str:
                    stop_str = self._format_time(stop)

            # Ensure start is before stop
            if start > stop:
                print(f"Swapping start/stop: {start} > {stop}")
                start, stop = stop, start
                start_str, stop_str = stop_str, start_str

            # Create a new segment with validated values
            valid_seg = {
                "start": start,
                "stop": stop,
                "start_str": start_str,
                "stop_str": stop_str,
            }

            print(f"Added valid segment: {valid_seg}")
            valid_segments.append(valid_seg)

        print(f"Validated {len(valid_segments)} of {len(segments)} segments")
        return valid_segments

    def _format_time(self, seconds):
        """Format time in seconds to HH:MM:SS.mmm format for FFmpeg."""
        if seconds is None:
            return ""

        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        seconds_part = seconds % 60

        # Format with millisecond precision for FFmpeg
        if hours > 0:
            return f"{hours:02d}:{minutes:02d}:{seconds_part:09.6f}"
        else:
            return f"{minutes:02d}:{seconds_part:09.6f}"

    def _extract_segment(self, input_file, segment, output_file, audio_filters=None):
        """Extract a single segment from the input file."""
        try:
            # Ensure paths are absolute
            input_file = os.path.abspath(input_file)
            output_file = os.path.abspath(output_file)

            # Log segment times for debugging
            logger.debug(
                f"Extracting from {segment['start_str']} to {segment['stop_str']}"
            )

            # Calculate and log expected segment duration
            expected_duration = segment["stop"] - segment["start"]
            logger.debug(f"Expected segment duration: {expected_duration:.2f} seconds")

            # Build command to extract segment - use -ss before -i for more accurate seeking
            cmd = [
                self.ffmpeg_path,
                "-y",  # Overwrite output
                "-v",
                "warning",  # Set verbosity level
                "-accurate_seek",  # Use accurate seeking mode
                "-ss",
                segment["start_str"],  # Start time (BEFORE input for faster seeking)
                "-i",
                input_file,  # Input file
                # Replace -to with -t (duration) for more precise segment extraction
                "-t",
                f"{expected_duration:.6f}",  # Duration in seconds (more precise than -to)
                "-avoid_negative_ts",
                "1",  # Avoid negative timestamps
                "-map_metadata",
                "-1",  # Remove metadata for cleaner output
            ]

            # Add any audio filters
            if audio_filters:
                cmd.extend(["-af", audio_filters])

            # Add output file
            cmd.append(output_file)

            # Log full command for debugging
            logger.debug(f"Extracting segment: {' '.join(cmd)}")

            # Run FFmpeg command with detailed output capturing
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            # Check if the process was successful
            if process.returncode != 0:
                logger.error(
                    f"Segment extraction failed with code {process.returncode}: {process.stderr}"
                )
                return False

            # Verify the output file was created and has content
            if not os.path.exists(output_file):
                logger.error(f"Output file not created: {output_file}")
                return False

            # Check file size (must be at least 100 bytes to be valid)
            file_size = os.path.getsize(output_file)
            if file_size < 100:
                logger.error(
                    f"Output file too small ({file_size} bytes): {output_file}"
                )
                return False

            logger.debug(
                f"Successfully extracted segment to {output_file} ({file_size} bytes)"
            )

            # Calculate and log expected segment duration
            expected_duration = segment["stop"] - segment["start"]
            logger.debug(f"Expected segment duration: {expected_duration:.2f} seconds")

            # After successful extraction, verify duration with ffprobe
            if os.path.exists(output_file) and os.path.getsize(output_file) > 100:
                try:
                    # Get actual duration of the extracted segment
                    ffprobe_cmd = [
                        self.ffmpeg_path.replace("ffmpeg", "ffprobe"),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        output_file,
                    ]
                    result = subprocess.run(ffprobe_cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        actual_duration = float(result.stdout.strip())
                        logger.debug(
                            f"Actual segment duration: {actual_duration:.2f} seconds (expected: {expected_duration:.2f})"
                        )

                        # Warn if duration differs significantly
                        if (
                            abs(actual_duration - expected_duration) > 1.0
                        ):  # Allow 1 second deviation
                            logger.warning(
                                f"Segment duration discrepancy: expected {expected_duration:.2f}s, got {actual_duration:.2f}s"
                            )
                except Exception as e:
                    logger.warning(f"Could not check segment duration: {str(e)}")

            return True

        except subprocess.TimeoutExpired:
            logger.error("FFmpeg segment extraction timed out after 300 seconds")
            return False
        except Exception as e:
            logger.exception(f"Error extracting segment: {str(e)}")
            return False

    def _concatenate_segments(self, segment_files, output_file):
        """Concatenate multiple segment files into one output file."""
        try:
            # Create concat file with absolute paths
            concat_file = os.path.join(os.path.dirname(output_file), "concat_list.txt")
            # Log all segment files being concatenated
            logger.debug(f"Preparing to concatenate {len(segment_files)} segments:")
            for i, segment in enumerate(segment_files):
                logger.debug(
                    f"  Segment {i + 1}: {segment} ({os.path.getsize(segment)} bytes)"
                )

            # First verify all segment files exist and have content
            missing_files = []
            empty_files = []
            for segment in segment_files:
                if not os.path.exists(segment):
                    missing_files.append(segment)
                elif os.path.getsize(segment) < 100:
                    empty_files.append(segment)

            if missing_files:
                logger.error(
                    f"Cannot concatenate: {len(missing_files)} segment files are missing"
                )
                for missing in missing_files:
                    logger.error(f"  Missing file: {missing}")
                return False

            if empty_files:
                logger.error(
                    f"Cannot concatenate: {len(empty_files)} segment files are empty or too small"
                )
                for empty in empty_files:
                    logger.error(f"  Empty/small file: {empty}")
                return False

            # Write relative paths to concat file (relative to concat file location)
            with open(concat_file, "w") as f:
                for segment in segment_files:
                    # Use absolute paths to avoid FFmpeg path resolution issues
                    abs_path = os.path.abspath(segment)
                    f.write(f"file '{abs_path}'\n")

            # Verify concat file was created
            if not os.path.exists(concat_file):
                logger.error(f"Failed to create concat list file: {concat_file}")
                return False

            # Log concat file contents for debugging
            with open(concat_file, "r") as f:
                logger.debug(f"Concat file contents:\n{f.read()}")

            # Build command to concatenate segments
            cmd = [
                self.ffmpeg_path,
                "-y",  # Overwrite output
                "-v",
                "warning",  # Set verbosity level
                "-f",
                "concat",  # Concat format
                "-safe",
                "0",  # Don't require safe filenames
                "-i",
                concat_file,  # Input concat file
                "-c",
                "copy",  # Copy streams without re-encoding
                output_file,  # Output file
            ]

            # Run FFmpeg command
            logger.debug(f"Concatenating segments: {' '.join(cmd)}")
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            # Check if concatenation was successful
            if process.returncode != 0:
                logger.error(
                    f"Concatenation failed with code {process.returncode}: {process.stderr}"
                )
                return False

            # Verify the output file exists and has valid content
            if not os.path.exists(output_file):
                logger.error(f"Concat output file not created: {output_file}")
                return False

            # Check output file size
            output_size = os.path.getsize(output_file)
            if output_size < 100:
                logger.error(f"Concat output file too small ({output_size} bytes)")
                return False

            logger.info(
                f"Successfully concatenated segments to {output_file} ({output_size} bytes)"
            )

            # After successful concatenation, verify total duration
            if os.path.exists(output_file) and os.path.getsize(output_file) > 100:
                try:
                    # Get actual duration of the final file
                    ffprobe_cmd = [
                        self.ffmpeg_path.replace("ffmpeg", "ffprobe"),
                        "-v",
                        "error",
                        "-show_entries",
                        "format=duration",
                        "-of",
                        "default=noprint_wrappers=1:nokey=1",
                        output_file,
                    ]
                    result = subprocess.run(ffprobe_cmd, capture_output=True, text=True)
                    if result.returncode == 0:
                        final_duration = float(result.stdout.strip())
                        logger.info(
                            f"Final concatenated file duration: {final_duration:.2f} seconds"
                        )
                except Exception as e:
                    logger.warning(f"Could not check final duration: {str(e)}")

            return True

        except Exception as e:
            logger.exception(f"Error concatenating segments: {str(e)}")
            return False
