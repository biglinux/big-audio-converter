"""Precise editing and packet-copy editing using the same process owner."""

import gettext
import json
import os
import time
from pathlib import Path

from .media import audio_duration, audio_stream, pcm_codec, probe_media
from .process import MediaError, ProcessRunner
from .profiles import LOUDNORM, metadata_args

# Every input costs FFmpeg a demuxer, a decoder and about 1 MB; near 2,300 it
# cannot open another decoder. Longer edits are joined through lossless parts.
CUTS_PER_RUN = 256
# The first pass's JSON keys, and the ranges FFmpeg accepts for them.
LOUDNESS_MEASUREMENTS = (
    ("measured_I", "input_i", -99, 0),
    ("measured_LRA", "input_lra", 0, 99),
    ("measured_TP", "input_tp", -99, 99),
    ("measured_thresh", "input_thresh", -99, 0),
    ("offset", "target_offset", -99, 99),
)


def command_error(stderr):
    """Keep technical stderr out of the primary user-facing message."""
    text = stderr.lower()
    if "no space left" in text:
        return MediaError(
            gettext.gettext(
                "There is not enough free disk space. Free some space and try again."
            ),
            stderr,
        )
    if "permission denied" in text:
        return MediaError(
            gettext.gettext(
                "The file or destination is not accessible. Check its permissions."
            ),
            stderr,
        )
    if "unknown encoder" in text or "encoder not found" in text:
        return MediaError(
            gettext.gettext(
                "The required audio encoder is unavailable. Install a compatible FFmpeg package."
            ),
            stderr,
        )
    if "no such filter" in text:
        return MediaError(
            gettext.gettext(
                "A selected audio effect is unavailable in this FFmpeg installation."
            ),
            stderr,
        )
    return MediaError(
        gettext.gettext(
            "The audio could not be processed. Check the file and selected output settings."
        ),
        stderr,
    )


def linear_loudnorm(stderr):
    """Build the second loudnorm pass from the first pass's report.

    Falls back to the single dynamic pass when the report is missing or out
    of range, as it is for silence (-inf).
    """
    try:
        report = json.loads(stderr[stderr.rindex("{") : stderr.rindex("}") + 1])
        values = [
            (option, float(report[key]), low, high)
            for option, key, low, high in LOUDNESS_MEASUREMENTS
        ]
    except (ValueError, KeyError, TypeError):
        return LOUDNORM
    if not all(low <= value <= high for _option, value, low, high in values):
        return LOUDNORM
    return (
        LOUDNORM
        + "".join(f":{option}={value}" for option, value, _low, _high in values)
        + ":linear=true"
    )


def progress_consumer(callback, duration, speed):
    """Turn FFmpeg's ``-progress`` output into fractions of ``duration``."""
    pending = bytearray()
    last_update = 0.0

    def consume(data):
        nonlocal last_update
        pending.extend(data)
        while b"\n" in pending:
            line, _, remainder = pending.partition(b"\n")
            pending[:] = remainder
            if line.startswith(b"out_time_us=") and callback and duration:
                try:
                    fraction = (
                        float(line.split(b"=", 1)[1]) / 1_000_000 * speed / duration
                    )
                except ValueError:
                    continue
                now = time.monotonic()
                if now - last_update >= 0.1:
                    callback(max(0.0, min(0.99, fraction)))
                    last_update = now
        if len(pending) > 65536:
            pending.clear()

    return consume


class SegmentProcessor:
    """Render the whole track or its cuts, or fail without publishing any output.

    Re-encoded cuts are decoded straight from the source and joined by the
    concat filter in one FFmpeg run: no codec priming or padding at the
    joins, and continuous effects, including loudness normalization, see the
    assembled timeline once. Packet copies cannot pass through a filter, so
    they are cut separately and joined by the concat demuxer.
    """

    def __init__(self, ffmpeg_path, runner=None):
        self.ffmpeg_path = ffmpeg_path
        self.runner = runner or ProcessRunner()
        self.last_output_info = None

    def render(
        self,
        source,
        info,
        stream,
        output,
        cuts=None,
        filters=(),
        params=("-c:a", "copy"),
        progress=None,
        speed=1,
        temp_dir=None,
    ):
        """Write the whole track, or validated ``cuts`` joined in order."""
        filters, params, index = list(filters), list(params), stream["index"]
        if params[params.index("-c:a") + 1] == "copy":
            if filters:
                raise MediaError(
                    gettext.gettext(
                        "Effects require re-encoding. Select an audio format instead of Fast Copy."
                    )
                )
            if cuts and len(cuts) > 1:
                return self._join_copies(
                    source, info, stream, output, cuts, params, temp_dir
                )
        inputs = [(source, index, cut) for cut in cuts or [None]]
        count, metadata = len(inputs), 0
        if count > CUTS_PER_RUN:
            parts = []
            for first in range(0, count, CUTS_PER_RUN):
                part = str(Path(temp_dir) / f"part-{first:06d}.nut")
                self.render(
                    source,
                    info,
                    stream,
                    part,
                    cuts[first : first + CUTS_PER_RUN],
                    params=["-f", "nut", "-c:a", pcm_codec(stream)],
                )
                parts.append((part, 0, None))
            # The source follows the parts only for its tags and artwork.
            inputs = parts + [(source, index, cuts[0])]
            count = metadata = len(parts)
        duration = (
            sum(cut["stop"] - cut["start"] for cut in cuts)
            if cuts
            else audio_duration(info, stream)
        )

        def ffmpeg(chain, level="error"):
            argv = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-n", "-v", level]
            for path, _track, cut in inputs:
                if cut:
                    length = f"{cut['stop'] - cut['start']:.9f}"
                    argv += ["-ss", cut["start_str"], "-t", length]
                argv += [
                    "-protocol_whitelist",
                    "file,pipe",
                    "-i",
                    os.path.abspath(path),
                ]
            if count > 1:
                chain = [f"concat=n={count}:v=0:a=1"] + chain
            if chain:
                labels = "".join(
                    f"[{number}:{track}]"
                    for number, (_path, track, _cut) in enumerate(inputs[:count])
                )
                argv += ["-filter_complex", labels + ",".join(chain) + "[out]"]
                argv += ["-map", "[out]"]
            else:
                argv += ["-map", f"0:{index}"]
            return argv + ["-nostats", "-progress", "pipe:1"]

        report = progress
        if LOUDNORM in filters:
            at = filters.index(LOUDNORM)
            # loudnorm only normalizes linearly with the whole timeline measured.
            measurement = self.runner.run(
                ffmpeg(filters[:at] + [LOUDNORM + ":print_format=json"], "info")
                + ["-f", "null", "-"],
                stdout_callback=progress_consumer(
                    progress and (lambda fraction: progress(fraction / 2)),
                    duration,
                    speed,
                ),
            )
            # loudnorm runs at 192 kHz; later filters work at the output rate.
            rate = params[params.index("-ar") + 1] if "-ar" in params else None
            filters[at : at + 1] = [linear_loudnorm(measurement.stderr)]
            filters.insert(at + 1, f"aresample={rate or stream['sample_rate']}")
            report = progress and (lambda fraction: progress(0.5 + fraction / 2))
        extension = Path(output).suffix.lstrip(".").lower()
        # Skip the audio mapping; ffmpeg() already mapped the rendered audio.
        argv = ffmpeg(filters) + metadata_args(info, stream, extension, metadata)[2:]
        if cuts:
            # Source chapters would keep their unedited times.
            argv += ["-map_chapters", "-1"]
        self._run(
            argv + params + [os.path.abspath(output)],
            progress_consumer(report, duration, speed),
        )
        self.validate_output(output)
        return output

    def _join_copies(self, source, info, stream, output, cuts, params, temp_dir):
        directory = Path(temp_dir).absolute()
        manifest = directory / "segments.ffconcat"
        # Generated relative basenames cannot contain quotes, newlines or
        # protocols. User paths remain separate argv elements throughout.
        with manifest.open("x", encoding="utf-8") as handle:
            handle.write("ffconcat version 1.0\n")
            for number, cut in enumerate(cuts):
                part = directory / f"segment-{number:06d}{Path(output).suffix}"
                if not part.name.replace("-", "").replace(".", "").isalnum():
                    raise MediaError(
                        gettext.gettext("An intermediate segment has an unsafe path.")
                    )
                self.render(source, info, stream, str(part), [cut], params=params)
                handle.write(f"file '{part.name}'\n")
        command = [
            self.ffmpeg_path,
            "-hide_banner",
            "-nostdin",
            "-n",
            "-v",
            "error",
            "-f",
            "concat",
            "-safe",
            "1",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            str(manifest),
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            os.path.abspath(source),
            "-map",
            "0:a:0",
            "-map_metadata",
            "1",
            "-map_metadata:s:a:0",
            f"1:s:{stream['index']}",
            "-map_chapters",
            "-1",
        ]
        metadata = metadata_args(info, stream, Path(output).suffix.lstrip("."), 1)
        # The joined audio is input zero; take only artwork mappings from the
        # original source, not its unedited audio.
        if "-c:v" in metadata:
            command += metadata[metadata.index("-c:v") - 2 :]
        self._run(command + params + [os.path.abspath(output)])
        self.validate_output(output)
        return output

    def _run(self, command, progress=None):
        result = self.runner.run(command, stdout_callback=progress)
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
            [
                self.ffmpeg_path,
                "-hide_banner",
                "-nostdin",
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-i",
                os.path.abspath(path),
                "-map",
                "0:a:0",
                "-frames:a",
                "1",
                "-f",
                "f32le",
                "pipe:1",
            ],
            timeout=15,
            stdout_callback=consume,
        )
        self.last_output_info = info
        if result.returncode or byte_count == 0:
            raise MediaError(
                gettext.gettext(
                    "The output contains no readable audio. No file was published."
                ),
                result.stderr,
            )
