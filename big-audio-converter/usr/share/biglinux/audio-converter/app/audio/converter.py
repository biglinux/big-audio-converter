# app/audio/converter.py

"""
Audio converter module for handling audio conversion with ffmpeg.
"""

import gettext
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from gi.repository import GLib

from .models import BatchResult, FileResult, MediaSource, Segment, finite_number
from .filters import build_audio_filters
from .process_runner import ProcessRunner, OperationCancelled
from .output_transaction import OutputTransaction
from .codec_profiles import build_codec_args, output_rate, ARTWORK_FORMATS, artwork_args

from .segment_processor import SegmentProcessor  # Import the segment processor

gettext.textdomain("big-audio-converter")
_ = gettext.gettext

logger = logging.getLogger(__name__)


class AudioConverter:
    """
    Audio converter using ffmpeg for conversion functionality.
    """

    def __init__(self, gtcrn_ladspa_path=None):
        """Initialize the audio converter."""
        self.ffmpeg_path = self._find_ffmpeg()
        if not self.ffmpeg_path:
            logger.error("ffmpeg not found! Audio conversion will not work.")

        self.gtcrn_ladspa_path = gtcrn_ladspa_path
        if self.gtcrn_ladspa_path and not os.path.exists(self.gtcrn_ladspa_path):
            logger.warning(f"GTCRN LADSPA plugin not found: {self.gtcrn_ladspa_path}")
            self.gtcrn_ladspa_path = None

        # Conversion properties
        self.cancel_flag = False
        self.current_process = None

    def _find_ffmpeg(self):
        """Discover executables without launching a process during GTK startup."""
        found = shutil.which("ffmpeg")
        if found:
            return found
        for candidate in ("/usr/lib/jellyfin-ffmpeg/ffmpeg", "/opt/local/bin/ffmpeg"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def convert_all_files(self, files, settings, progress_callback, finish_callback):
        """Process a stable request snapshot and report every input's final state."""
        import copy
        files = list(files)
        self.last_batch = BatchResult()
        try:
            self.reset_cancellation()
            snapshot = copy.deepcopy(settings)
            for index, identifier in enumerate(files):
                if self.cancel_flag:
                    self.last_batch.files.extend(FileResult(path, "cancelled", message="Not processed because the batch was cancelled.") for path in files[index:])
                    break
                output = self._get_output_path(identifier, snapshot["format"])
                def progress(value, index=index, identifier=identifier):
                    self._dispatch(progress_callback, index, identifier, value)
                self.convert_file(identifier, output, snapshot, progress)
                self.last_batch.files.append(self.last_result)
            succeeded = self.last_batch.successful_sources
            failed = len(self.last_batch.failed_sources)
            cancelled = sum(item.status == "cancelled" for item in self.last_batch.files)
            message = f"{len(succeeded)} completed; {failed} failed; {cancelled} cancelled."
            self._dispatch(finish_callback, bool(succeeded), message, succeeded)
        except Exception as error:
            # This is the worker's presentation boundary. Preserve diagnostics
            # while ensuring an unexpected error cannot strand a progress dialog.
            logger.exception("Batch conversion failed")
            self._dispatch(finish_callback, False, str(error), self.last_batch.successful_sources)

    def _convert_file_staged(self, input_path, output_path, settings, progress_callback=None):
        """Convert a single file with the given settings.

        Supports extracting specific audio tracks from video files using track metadata.
        """
        temp_dir = None
        try:
            # Check if this is a virtual track path (format: video_path::track1.ext)
            track_metadata = None
            actual_input_path = os.path.abspath(input_path)

            if "::" in input_path and not os.path.isfile(input_path):
                # This is a track extraction request
                logger.info(f"Detected track extraction request: {input_path}")
                if (
                    "track_metadata" in settings
                    and input_path in settings["track_metadata"]
                ):
                    track_metadata = settings["track_metadata"][input_path]
                    actual_input_path = os.path.abspath(track_metadata["source_video"])
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

            output_dir = os.path.dirname(output_path)
            if output_dir and not os.path.exists(output_dir):
                os.makedirs(output_dir, exist_ok=True)

            # Build ffmpeg command
            cmd = [self.ffmpeg_path, "-y", "-i", actual_input_path, "-map_metadata", "0"]

            if track_metadata:
                track_index = track_metadata["track_index"]
                cmd.extend(["-map", f"0:{track_index}"])
                logger.info(f"Mapping audio stream 0:{track_index}")

            if not track_metadata:
                cmd.extend(["-map", "0:a:0"])
            cmd.extend(artwork_args(settings.get("_source_probe", {}), output_path, format_hint=settings.get("format")))

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
                audio_filters = self._build_audio_filters(settings)
                if audio_filters:
                    cmd.extend(["-af", ",".join(audio_filters)])
                cmd.extend(self._build_codec_args(settings))

            # Apply channel limit if specified (1=mono, 2=stereo)
            channels = settings.get("channels")
            if channels and settings["format"] != "copy":
                cmd.extend(["-ac", str(channels)])

            # Handle cut functionality
            if file_has_segments:
                segments = settings["cut_segments"]
                if segments:
                    temp_dir = tempfile.mkdtemp(prefix="audioconv_")
                    return self._convert_segments(
                        actual_input_path, segments, settings, audio_filters,
                        track_metadata, output_path, channels, temp_dir,
                    )
                    # Fall through to normal conversion if no valid segments

            # Normal conversion (no segments)
            cmd.append(output_path)
            logger.debug(f"FFmpeg command: {' '.join(cmd)}")

            duration = self._get_duration(actual_input_path) or 0
            speed = float(settings.get("speed", 1.0)) if settings.get("format") != "copy" else 1.0
            if not self._run_ffmpeg(cmd, duration / speed, progress_callback):
                return False

            logger.info(f"Conversion successful: {input_path} -> {output_path}")
            return not self.cancel_flag

        except Exception as e:
            logger.exception(f"Error during conversion: {str(e)}")
            return False
        finally:
            self.current_process = None
            self._cleanup_temp_dir(temp_dir)

    def _build_audio_filters(self, settings):
        return build_audio_filters(settings, self.gtcrn_ladspa_path)

    def _build_codec_args(self, settings, channels=None):
        return build_codec_args(settings, channels, settings.get("_source_stream"))

    @staticmethod
    def _cleanup_temp_dir(temp_dir):
        """Clean up a temporary directory, logging any errors."""
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
                logger.debug("Temporary directory cleaned up")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory: {e}")

    def _convert_segments(self, actual_input_path, segments, settings, audio_filters,
                          track_metadata, output_path, channels, temp_dir):
        """Handle segment-based conversion (cut mode). Returns True on success."""
        segment_processor = SegmentProcessor(self.ffmpeg_path, self._get_runner())
        segment_processor.source_info = settings.get("_source_probe", {})
        segment_processor.source_file = actual_input_path
        segment_output_format = settings["format"]
        segment_codec_params = None

        if segment_output_format == "copy":
            _, output_ext = os.path.splitext(output_path)
            segment_output_format = output_ext[1:].lower() if output_ext else "mp3"
        else:
            segment_codec_params = self._build_codec_args(settings, channels)

        cut_merge = settings.get("cut_merge", True)
        filter_str = ",".join(audio_filters) if audio_filters else None
        track_index = track_metadata.get("track_index") if track_metadata else None

        if not cut_merge and len(segments) > 1:
            # Separate files mode
            base_name, base_ext = os.path.splitext(output_path)
            all_ok = True
            for seg_idx, segment in enumerate(segment_processor._validate_segments(segments)):
                seg_output = f"{base_name}_segment{seg_idx + 1}{base_ext}"
                if not segment_processor._extract_segment(
                    actual_input_path, segment, seg_output,
                    filter_str, track_index, segment_codec_params,
                ):
                    logger.error(f"Failed to extract segment {seg_idx + 1} to {seg_output}")
                    all_ok = False
            self._cleanup_temp_dir(temp_dir)
            return all_ok

        # Merge mode (default)
        processed_output = segment_processor.process_segments(
            actual_input_path, segments, segment_output_format, temp_dir,
            filter_str, track_index, output_path, segment_codec_params,
        )
        if not processed_output or not os.path.exists(processed_output):
            logger.error("Segment processing failed.")
            self._cleanup_temp_dir(temp_dir)
            return False

        if os.path.abspath(processed_output) != os.path.abspath(output_path):
            try:
                shutil.copy2(processed_output, output_path)
            finally:
                self._cleanup_temp_dir(temp_dir)
        else:
            self._cleanup_temp_dir(temp_dir)

        logger.info(f"Conversion with segments successful: {actual_input_path} -> {output_path}")
        return True

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
        try:
            info = self._probe_media(file_path)
            value = float(info.get("format", {}).get("duration", 0))
            return value if math.isfinite(value) and value > 0 else 0
        except (OSError, ValueError, subprocess.SubprocessError, OperationCancelled):
            return 0

    def _get_output_path(self, input_path, output_format):
        """Generate the output path based on input path and format."""
        # Handle virtual track paths (format: video_path::trackN.ext)
        if "::" in input_path and not os.path.isfile(input_path):
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


        return output_path

    def cancel_conversion(self):
        """Request cancellation; the worker retains ownership until it reaps the child."""
        self.cancel_flag = True
        runner = getattr(self, "_runner", None)
        if runner is not None:
            runner.cancel()

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

    def _get_runner(self):
        if not hasattr(self, "_runner"):
            self._runner = ProcessRunner(lambda: self.cancel_flag)
        return self._runner

    def _probe_media(self, path):
        """Read bounded JSON metadata with a cancellable ten-second deadline."""
        candidate = str(Path(self.ffmpeg_path or "ffmpeg").with_name("ffprobe"))
        ffprobe = candidate if os.path.isfile(candidate) else shutil.which("ffprobe")
        if not ffprobe:
            raise ValueError("FFprobe is not installed")
        result = self._get_runner().run(
            [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", os.path.abspath(path)],
            timeout=10,
        )
        if result.returncode:
            self.last_diagnostics = result.stderr
            raise ValueError("The media could not be read. It may be damaged or unsupported.")
        return json.loads(result.stdout)

    def _run_ffmpeg(self, command, duration=0, progress_callback=None):
        """Drain both pipes even when duration or a progress callback is absent."""
        pending = bytearray()
        def consume(data):
            pending.extend(data)
            while b"\n" in pending:
                line, _, rest = pending.partition(b"\n")
                pending[:] = rest
                if line.startswith(b"out_time_us=") and duration and progress_callback:
                    try:
                        position = float(line.partition(b"=")[2]) / 1_000_000
                    except ValueError:
                        continue
                    progress_callback(max(0.0, min(position / duration, 0.99)))
            if len(pending) > 8192:
                raise ValueError("Invalid FFmpeg progress stream")
        cmd = command[:1] + ["-hide_banner", "-nostdin", "-loglevel", "error", "-xerror", "-nostats", "-progress", "pipe:1"] + command[1:]
        result = self._get_runner().run(cmd, stdout_consumer=consume)
        self.last_diagnostics = result.stderr
        return result.returncode == 0 and not self.cancel_flag

    def reset_cancellation(self):
        """Start a new operation only after the previous process has finished."""
        self._get_runner().reset()
        self.cancel_flag = False

    def convert_file(self, input_path, output_path, settings, progress_callback=None):
        """Convert an immutable request and publish only validated complete outputs."""
        identifier = os.fspath(input_path)
        options = dict(settings)
        self.last_diagnostics = ""
        try:
            if self.cancel_flag:
                raise OperationCancelled()
            if not self.ffmpeg_path:
                raise ValueError("FFmpeg is not installed")
            if not os.fspath(output_path) or "\x00" in os.fspath(output_path):
                raise ValueError("A valid output filename is required")
            source = MediaSource.resolve(identifier, options.get("track_metadata"))
            if not os.path.isfile(source.path):
                raise ValueError("The input is not a readable local file")
            info = self._probe_media(source.path)
            streams = [stream for stream in info.get("streams", [])
                       if stream.get("codec_type") == "audio"
                       and (source.stream_index is None or stream["index"] == source.stream_index)]
            if not streams:
                raise ValueError("The selected file or stream does not contain audio")
            stream = streams[0]
            options["_source_stream"] = stream
            options["_source_probe"] = info
            value = stream.get("duration") or info.get("format", {}).get("duration")
            duration = float(value) if value not in (None, "N/A") else None
            if duration is not None and (not math.isfinite(duration) or duration <= 0):
                duration = None
            raw = []
            if options.get("cut_enabled"):
                raw = options.get("file_markers", {}).get(identifier, options.get("cut_segments", []))
            if not isinstance(raw, (list, tuple)) or len(raw) > 256:
                raise ValueError("Provide at most 256 valid segments")
            segments = [Segment.from_mapping(item, duration).as_dict() for item in raw]
            separate = bool(segments) and not options.get("cut_merge", True) and len(segments) > 1
            base, extension = os.path.splitext(os.fspath(output_path))
            destinations = ([f"{base}_segment{index + 1}{extension}" for index in range(len(segments))]
                            if separate else [os.fspath(output_path)])
            warnings = []
            fmt = options.get("format", "mp3")
            if fmt == "copy" and segments:
                warnings.append("Stream-copy cuts are approximate and follow codec packet boundaries.")
            if fmt != "copy" and output_rate(options, stream) != int(stream.get("sample_rate", 48000)):
                warnings.append("The output codec requires a different sample rate.")
            if fmt == "copy":
                artwork_format = extension.lstrip(".").lower()
            else:
                artwork_format = fmt
            if any(s.get("disposition", {}).get("attached_pic") for s in info.get("streams", [])) and artwork_format not in ARTWORK_FORMATS:
                warnings.append("This output container does not preserve the embedded cover image.")
            with OutputTransaction(destinations) as transaction:
                for index, staged in enumerate(transaction.staged):
                    current = dict(options)
                    current_segments = [segments[index]] if separate else segments
                    current["file_markers"] = {identifier: current_segments}
                    current["cut_merge"] = True
                    def progress(value, index=index):
                        if progress_callback is not None:
                            progress_callback(min(0.99, (index + value) / len(destinations)))
                    if not self._convert_file_staged(identifier, staged, current, progress):
                        if self.cancel_flag:
                            raise OperationCancelled()
                        raise RuntimeError("Audio processing failed. Check the format, write permission and free disk space.")
                    output_info = self._probe_media(staged)
                    if not any(s.get("codec_type") == "audio" for s in output_info.get("streams", [])) or os.path.getsize(staged) == 0:
                        raise RuntimeError("The encoder did not produce a valid audio output")
                if self.cancel_flag:
                    raise OperationCancelled()
                outputs = transaction.commit()
            self.last_result = FileResult(identifier, "success", outputs, warnings=tuple(warnings))
            if progress_callback is not None:
                progress_callback(1.0)
            return True
        except OperationCancelled:
            self.last_result = FileResult(identifier, "cancelled", message="Conversion cancelled; unfinished outputs were removed.")
            return False
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.SubprocessError) as error:
            self.last_result = FileResult(identifier, "failed", message=str(error), diagnostics=self.last_diagnostics)
            logger.error("Conversion failed: %s", error)
            return False

    @staticmethod
    def _dispatch(callback, *args):
        if callback is None:
            return
        def invoke():
            callback(*args)
            return False
        GLib.idle_add(invoke)
