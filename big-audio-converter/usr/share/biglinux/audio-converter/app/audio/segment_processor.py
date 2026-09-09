# app/audio/segment_processor.py

"""
Segment processor for handling audio segments during conversion.
"""

import logging
import json
import math
from pathlib import Path
import tempfile

from .models import Segment, format_timestamp
from .process_runner import ProcessRunner, OperationCancelled
from .output_transaction import OutputTransaction
from .codec_profiles import artwork_args, COPY_MUXERS
import os
import subprocess
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class SegmentProcessor:
    """
    Helper class for processing audio segments during conversion.
    Ensures all segments are properly cut and combined.
    """

    def __init__(self, ffmpeg_path, runner=None):
        self.ffmpeg_path = ffmpeg_path
        self.runner = runner if runner is not None else ProcessRunner()
        self.source_info = {}

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
        if not segments or len(segments) == 0:
            logger.warning("No segments provided for processing")
            return None

        logger.info(f"Processing {len(segments)} segments from {input_file}")

        # Original segment order for debugging
        logger.debug(
            f"Original segment order: {[(s.get('segment_index', '?'), s.get('start_str', s.get('start'))) for s in segments]}"
        )

        # Filter out invalid segments
        valid_segments = self._validate_segments(segments)
        if len(valid_segments) != len(segments):
            logger.error("Every requested segment must be valid")
            return None
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
                return None

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
        """Normalize valid intervals; callers must reject an incomplete result."""
        if not isinstance(segments, (list, tuple)) or len(segments) > 256:
            return []
        valid = []
        for item in segments:
            try:
                valid.append(Segment.from_mapping(item).as_dict())
            except (AttributeError, TypeError, ValueError):
                continue
        return valid

    def _format_time(self, seconds):
        return format_timestamp(seconds)

    def _extract_segment(self, input_file, segment, output_file, audio_filters=None,
                         track_index=None, codec_params=None):
        """Extract into a private file, then publish without replacing anything."""
        try:
            interval = Segment.from_mapping(segment)
            with OutputTransaction([output_file], rename_on_conflict=False) as transaction:
                staged = transaction.staged[0]
                command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-y",
                           "-ss", f"{interval.start:.9f}", "-t", f"{interval.duration:.9f}",
                           "-i", os.path.abspath(input_file),
                           "-map", f"0:{track_index}" if track_index is not None else "0:a:0",
                           "-map_metadata", "0"]
                source_info = getattr(self, "source_info", {})
                command.extend(artwork_args(source_info, output_file))
                if codec_params or audio_filters:
                    chain = [f"atrim=duration={interval.duration:.9f}", "asetpts=PTS-STARTPTS"]
                    if audio_filters:
                        chain.append(audio_filters)
                    command.extend(["-af", ",".join(chain)])
                    if codec_params:
                        command.extend(codec_params)
                else:
                    extension = os.path.splitext(output_file)[1].lstrip(".").lower()
                    command.extend(["-c:a", "copy", "-f", COPY_MUXERS.get(extension, extension)])
                command.append(staged)
                result = self.runner.run(command)
                if result.returncode:
                    logger.error("Segment extraction failed: %s", result.stderr)
                    return False
                if not self._output_has_audio(staged):
                    return False
                transaction.commit()
            return True
        except (OSError, ValueError, subprocess.SubprocessError, OperationCancelled) as error:
            logger.error("Segment extraction did not complete: %s", error)
            return False

    def _concatenate_segments(self, segment_files, output_file):
        """Use private ASCII list entries, avoiding filename escaping ambiguities."""
        if not segment_files:
            return False
        try:
            with OutputTransaction([output_file], rename_on_conflict=False) as transaction:
                parent = os.path.dirname(transaction.staged[0])
                with tempfile.TemporaryDirectory(prefix="concat-", dir=parent) as directory:
                    entries = []
                    for index, source in enumerate(segment_files):
                        name = f"part{index:04d}"
                        os.symlink(os.path.abspath(source), os.path.join(directory, name))
                        entries.append(f"file '{name}'\n")
                    playlist = os.path.join(directory, "list.txt")
                    Path(playlist).write_text("".join(entries), encoding="utf-8")
                    original = getattr(self, "source_file", segment_files[0])
                    command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-y",
                               "-f", "concat", "-safe", "1", "-i", playlist, "-i", os.path.abspath(original),
                               "-map", "0:a:0", "-map_metadata", "1", "-c:a", "copy"]
                    command.extend(artwork_args(self.source_info, output_file, input_index=1))
                    command.append(transaction.staged[0])
                    result = self.runner.run(command)
                    if result.returncode:
                        logger.error("Segment concatenation failed: %s", result.stderr)
                        return False
                    if not self._output_has_audio(transaction.staged[0]):
                        return False
                    transaction.commit()
            return True
        except (OSError, ValueError, subprocess.SubprocessError, OperationCancelled) as error:
            logger.error("Segment concatenation did not complete: %s", error)
            return False

    def _output_has_audio(self, path):
        """Reject empty headers, including containers with substantial metadata."""
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            return False
        candidate = str(Path(self.ffmpeg_path).with_name("ffprobe"))
        ffprobe = candidate if os.path.isfile(candidate) else "ffprobe"
        result = self.runner.run([ffprobe, "-v", "error", "-select_streams", "a:0",
                                  "-show_entries", "stream=duration:format=duration", "-of", "json", path], timeout=10)
        if result.returncode:
            return False
        info = json.loads(result.stdout)
        if not info.get("streams"):
            return False
        durations = [s.get("duration") for s in info["streams"]] + [info.get("format", {}).get("duration")]
        for value in durations:
            if value not in (None, "N/A"):
                duration = float(value)
                if math.isfinite(duration) and duration > 0:
                    return True
        result = self.runner.run([ffprobe, "-v", "error", "-select_streams", "a:0", "-show_packets",
                                  "-read_intervals", "%+#1", "-of", "json", path], timeout=10)
        return result.returncode == 0 and bool(json.loads(result.stdout).get("packets"))
