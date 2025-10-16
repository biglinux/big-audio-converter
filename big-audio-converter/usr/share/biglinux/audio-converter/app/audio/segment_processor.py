# app/audio/segment_processor.py

"""
Segment processor for handling audio segments during conversion.
"""

import os
import logging
import subprocess
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class SegmentProcessor:
    """
    Helper class for processing audio segments during conversion.
    Ensures all segments are properly cut and combined.
    """

    def __init__(self, ffmpeg_path):
        self.ffmpeg_path = ffmpeg_path

    def process_segments(
        self,
        input_file: str,
        segments: List[Dict],
        output_format: str,
        temp_dir: str,
        audio_filters: Optional[str] = None,
        track_index: Optional[int] = None,
        final_output_path: Optional[str] = None,
        codec_params: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Process multiple segments and combine them into a single output

        Args:
            input_file: Path to the input audio/video file
            segments: List of segment dictionaries with start, stop, etc.
            output_format: Desired output format (e.g., 'mp3', 'wav')
            temp_dir: Directory for temporary files
            audio_filters: Optional audio filters to apply
            track_index: Optional absolute stream index for video track extraction
            final_output_path: Optional final output path for direct extraction (optimization)
            codec_params: Optional codec parameters (e.g., ['-c:a', 'aac', '-b:a', '192k'])

        Returns:
            Path to the processed output file or None if processing failed
        """
        """
        Process multiple segments from input file and return path to the processed output.

        Args:
            input_file: Path to the input audio file
            segments: List of segment dictionaries with start and stop times
            output_format: Output audio format (mp3, ogg, etc.)
            temp_dir: Directory for temporary files
            audio_filters: Optional audio filters to apply
            track_index: Optional absolute stream index for video track extraction

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

        # Optimization: For single segment with no filters, extract directly to final destination
        if len(valid_segments) == 1 and not audio_filters and final_output_path:
            logger.info(
                f"Single segment extraction - writing directly to final destination: {final_output_path}"
            )
            segment = valid_segments[0]
            if self._extract_segment(
                input_file,
                segment,
                final_output_path,
                audio_filters,
                track_index,
                codec_params,
            ):
                logger.debug(
                    f"Successfully extracted segment directly to {final_output_path}"
                )
                return final_output_path
            else:
                logger.error("Failed to extract segment to final destination")
                return None

        # Process each segment separately to temp directory (multi-segment or filters case)
        temp_segments = []

        for i, segment in enumerate(valid_segments):
            segment_output = os.path.join(temp_dir, f"segment_{i}.{output_format}")

            if self._extract_segment(
                input_file,
                segment,
                segment_output,
                audio_filters,
                track_index,
                codec_params,
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
                print("Missing time strings, using numeric values")
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

        # Always use HH:MM:SS.mmm format for FFmpeg compatibility
        return f"{hours:02d}:{minutes:02d}:{seconds_part:09.6f}"

    def _extract_segment(
        self,
        input_file,
        segment,
        output_file,
        audio_filters=None,
        track_index=None,
        codec_params=None,
    ):
        """Extract a single segment from the input file.

        Args:
            input_file: Path to input file
            segment: Segment dictionary with start/stop times
            output_file: Path to output file
            audio_filters: Optional audio filters string
            track_index: Optional absolute stream index for track extraction
            codec_params: Optional codec parameters list (e.g., ['-c:a', 'aac', '-b:a', '192k'])
        """
        try:
            # Ensure paths are absolute
            input_file = os.path.abspath(input_file)
            output_file = os.path.abspath(output_file)

            # Calculate expected segment duration for the -t flag
            expected_duration = segment["stop"] - segment["start"]
            logger.debug(
                f"Extracting from {segment['start_str']} for duration {expected_duration:.6f}s"
            )

            # Build command to extract segment
            cmd = [
                self.ffmpeg_path,
                "-vn",
                "-sn",
                "-y",  # Overwrite output
                "-v",
                "warning",  # Set verbosity level
                "-accurate_seek",
                "-ss",
                segment["start_str"],  # Start time (BEFORE input for faster seeking)
                "-i",
                input_file,  # Input file
                "-t",
                f"{expected_duration:.6f}",  # Duration is more precise than -to
                "-avoid_negative_ts",
                "1",
                "-map_metadata",
                "-1",  # Remove metadata for cleaner output
            ]

            # If track_index is provided, add -map option for stream selection
            if track_index is not None:
                cmd.extend(["-map", f"0:{track_index}"])
                logger.debug(f"Mapping stream 0:{track_index} for segment extraction")

            # Add codec parameters if provided, otherwise use copy mode or filters
            if codec_params:
                # Use provided codec parameters (e.g., for AAC encoding)
                cmd.extend(codec_params)
                logger.debug(f"Using codec params: {codec_params}")
            elif audio_filters:
                # Apply audio filters (requires encoding)
                cmd.extend(["-af", audio_filters])
            else:
                # Copy mode - use codec copy
                cmd.extend(["-c:a", "copy"])

                # When extracting from video source, force output format
                # CRITICAL: -f must come AFTER -c:a copy, but BEFORE output path
                if track_index is not None:
                    _, ext = os.path.splitext(output_file)
                    ext_lower = ext[1:].lower() if ext else ""

                    format_map = {
                        "eac3": "eac3",
                        "ac3": "ac3",
                        "dts": "dts",
                        "flac": "flac",
                        "aac": "adts",
                        "mp3": "mp3",
                        "opus": "opus",
                        "ogg": "ogg",
                    }

                    if ext_lower in format_map:
                        cmd.extend(["-f", format_map[ext_lower]])
                        logger.debug(
                            f"Forcing {format_map[ext_lower]} format for video track extraction"
                        )

            # Add output file
            cmd.append(output_file)

            # Log full command for debugging
            print("\n=== SEGMENT EXTRACTION COMMAND ===")
            print(f"{' '.join(cmd)}")
            print("===================================\n")
            logger.info("=== SEGMENT EXTRACTION COMMAND ===")
            logger.info(f"{' '.join(cmd)}")
            logger.info("===================================")
            logger.debug(f"Extracting segment: {' '.join(cmd)}")

            # Run FFmpeg command
            process = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=300,  # 5 minute timeout
            )

            if process.returncode != 0:
                logger.error(
                    f"Segment extraction failed with code {process.returncode}: {process.stderr}"
                )
                return False

            if not os.path.exists(output_file) or os.path.getsize(output_file) < 100:
                logger.error(f"Output file is missing or empty: {output_file}")
                return False

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
            logger.debug(f"Preparing to concatenate {len(segment_files)} segments:")

            with open(concat_file, "w") as f:
                for segment in segment_files:
                    abs_path = os.path.abspath(segment)
                    f.write(f"file '{abs_path}'\n")

            # Build command to concatenate segments
            cmd = [
                self.ffmpeg_path,
                "-vn",
                "-sn",
                "-y",  # Overwrite output
                "-v",
                "warning",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                concat_file,
                "-c",
                "copy",
                output_file,
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

            if process.returncode != 0:
                logger.error(
                    f"Concatenation failed with code {process.returncode}: {process.stderr}"
                )
                return False

            if not os.path.exists(output_file) or os.path.getsize(output_file) < 100:
                logger.error(f"Concat output file is missing or empty: {output_file}")
                return False

            logger.info(f"Successfully concatenated segments to {output_file}")
            return True

        except Exception as e:
            logger.exception(f"Error concatenating segments: {str(e)}")
            return False
