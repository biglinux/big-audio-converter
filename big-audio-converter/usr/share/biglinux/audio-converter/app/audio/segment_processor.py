"""Precise editing and packet-copy editing using the same process owner."""

import gettext
import math
import os
from pathlib import Path

from .media import Segment, audio_duration, audio_stream, pcm_codec, probe_media
from .process import MediaError, ProcessRunner
from .profiles import metadata_args


def command_error(stderr):
    """Keep technical stderr out of the primary user-facing message."""
    text = stderr.lower()
    if "no space left" in text:
        return MediaError(gettext.gettext('There is not enough free disk space. Free some space and try again.'), stderr)
    if "permission denied" in text:
        return MediaError(gettext.gettext('The file or destination is not accessible. Check its permissions.'), stderr)
    if "unknown encoder" in text or "encoder not found" in text:
        return MediaError(gettext.gettext('The required audio encoder is unavailable. Install a compatible FFmpeg package.'), stderr)
    if "no such filter" in text:
        return MediaError(gettext.gettext('A selected audio effect is unavailable in this FFmpeg installation.'), stderr)
    return MediaError(gettext.gettext('The audio could not be processed. Check the file and selected output settings.'), stderr)


class SegmentProcessor:
    """Extract every requested segment or fail without publishing any output.

    Re-encoded merges use lossless intermediates and one final encode. This
    avoids codec priming/padding at every join and applies continuous effects,
    including loudness normalization, to the assembled timeline only once.
    Intermediate files trade disk I/O for bounded memory with reordered or
    overlapping segments. No filter is silently combined with stream copy.
    """

    def __init__(self, ffmpeg_path, runner=None):
        self.ffmpeg_path = ffmpeg_path
        self.runner = runner or ProcessRunner()
        self.last_output_info = None

    def process_segments(self, input_file, segments, output_format, temp_dir,
                         audio_filters=None, track_index=None, final_output_path=None,
                         codec_params=None):
        info = probe_media(input_file, self.runner, self.ffmpeg_path)
        stream = audio_stream(info, track_index)
        selected = self._validate_segments(segments, audio_duration(info, stream), int(stream["sample_rate"]))
        if not selected:
            raise MediaError(gettext.gettext('Mark at least one valid segment before exporting.'))
        output = final_output_path or str(Path(temp_dir) / f"combined_output.{output_format}")
        if len(selected) == 1:
            self._extract_segment(input_file, selected[0], output, audio_filters, stream["index"], codec_params, info)
            return output
        if audio_filters and not codec_params:
            raise MediaError(gettext.gettext('Effects require re-encoding. Select an audio format instead of Fast Copy.'))
        intermediates = []
        for index, segment in enumerate(selected):
            self.runner.check_cancelled()
            if codec_params:
                params = ["-f", "nut", "-c:a", pcm_codec(stream)]
                extension = "nut"
            else:
                params = None
                extension = output_format
            target = str(Path(temp_dir) / f"segment-{index:06d}.{extension}")
            self._extract_segment(input_file, segment, target, None, stream["index"], params, info, artwork=False)
            intermediates.append(target)
        self._concatenate_segments(intermediates, output, temp_dir, codec_params, audio_filters, input_file, info, stream)
        return output

    @staticmethod
    def _validate_segments(segments, duration=None, sample_rate=None):
        return [Segment.from_mapping(segment, duration, sample_rate).as_mapping() for segment in segments]

    @staticmethod
    def _format_time(seconds):
        if seconds is None:
            return ""
        value = float(seconds)
        if value < 0 or not math.isfinite(value):
            raise ValueError("Time must be finite and nonnegative")
        hours, remainder = divmod(value, 3600)
        minutes, remainder = divmod(remainder, 60)
        return f"{int(hours):02d}:{int(minutes):02d}:{remainder:09.6f}"

    def _extract_segment(self, input_file, segment, output_file, audio_filters=None,
                         track_index=None, codec_params=None, info=None, artwork=True):
        info = info or probe_media(input_file, self.runner, self.ffmpeg_path)
        stream = audio_stream(info, track_index)
        selected = Segment.from_mapping(segment, audio_duration(info, stream), int(stream["sample_rate"]))
        if audio_filters and not codec_params:
            raise MediaError(gettext.gettext('Effects require re-encoding. Select an audio format instead of Fast Copy.'))
        command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-n", "-v", "error", "-xerror",
                   "-ss", f"{selected.start:.9f}", "-t", f"{selected.stop - selected.start:.9f}",
                   "-protocol_whitelist", "file,pipe", "-i", os.path.abspath(input_file)]
        extension = Path(output_file).suffix.lstrip(".").lower()
        command += metadata_args(info, stream, extension if artwork else "")
        if audio_filters:
            command += ["-af", audio_filters]
        command += codec_params or ["-c:a", "copy"]
        command += [os.path.abspath(output_file)]
        self._run(command)
        self.validate_output(output_file)
        return True

    def _concatenate_segments(self, segment_files, output_file, temp_dir,
                              codec_params=None, audio_filters=None, metadata_source=None,
                              info=None, stream=None):
        if not segment_files:
            raise MediaError(gettext.gettext('No complete audio segments were produced.'))
        directory = Path(temp_dir).absolute()
        manifest = directory / "segments.ffconcat"
        # Generated relative basenames cannot contain quotes, newlines or
        # protocols. User paths remain separate argv elements throughout.
        with manifest.open("x", encoding="utf-8") as handle:
            handle.write("ffconcat version 1.0\n")
            for source in segment_files:
                source = Path(source).absolute()
                if source.parent != directory or not source.name.startswith("segment-") or not source.name.replace("-", "").replace(".", "").isalnum():
                    raise MediaError(gettext.gettext('An intermediate segment has an unsafe path.'))
                handle.write(f"file '{source.name}'\n")
        command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-n", "-v", "error", "-xerror",
                   "-f", "concat", "-safe", "1", "-protocol_whitelist", "file,pipe", "-i", str(manifest)]
        if metadata_source:
            command += ["-protocol_whitelist", "file,pipe", "-i", os.path.abspath(metadata_source), "-map", "0:a:0",
                        "-map_metadata", "1", "-map_metadata:s:a:0", f"1:s:{stream['index']}"]
            metadata = metadata_args(info, stream, Path(output_file).suffix.lstrip("."), 1)
            # The concatenated audio is input zero; take only artwork mappings
            # from the original source, not its unedited audio.
            if "-c:v" in metadata:
                start = metadata.index("-c:v") - 2
                command += metadata[start:]
        else:
            command += ["-map", "0:a:0", "-map_metadata", "0"]
        if audio_filters:
            command += ["-af", audio_filters]
        command += codec_params or ["-c:a", "copy"]
        command += [os.path.abspath(output_file)]
        self._run(command)
        self.validate_output(output_file)
        return True

    def _run(self, command):
        result = self.runner.run(command)
        if result.returncode:
            raise command_error(result.stderr)
        return result

    def validate_output(self, path):
        """Require a readable audio stream and at least one decoded frame."""
        info = probe_media(path, self.runner, self.ffmpeg_path)
        audio_stream(info)
        byte_count = 0

        def consume(data):
            nonlocal byte_count
            byte_count += len(data)

        result = self.runner.run(
            [self.ffmpeg_path, "-hide_banner", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", os.path.abspath(path),
             "-map", "0:a:0", "-frames:a", "1", "-f", "f32le", "pipe:1"],
            timeout=15, stdout_callback=consume,
        )
        self.last_output_info = info
        if result.returncode or byte_count == 0:
            raise MediaError(gettext.gettext('The output contains no readable audio. No file was published.'), result.stderr)
