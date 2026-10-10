"""Audio conversion with explicit jobs, validated plans and safe output files."""

import gettext
import logging
import os
import shutil
import threading
from copy import deepcopy
from pathlib import Path

from app.main_loop import MainLoopSources

from .media import (
    BatchResult,
    ConversionResult,
    MediaSource,
    Segment,
    audio_duration,
    audio_stream,
    complement,
    file_identity,
    probe_media,
)
from .output import available_path, publish_all, staging_directory
from .process import CancelledError, MediaError, ProcessRunner
from .profiles import (
    build_audio_filters,
    codec_args,
    copy_container,
    validate_settings,
)
from .segment_renderer import SegmentRenderer

logger = logging.getLogger(__name__)


class AudioConverter:
    def __init__(self, noise_plugins=None):
        self.ffmpeg_path = self._find_ffmpeg()
        self.noise_plugins = dict(noise_plugins or {})
        # Ships with dpdfnet-native; without it export uses the live plugin.
        self.offline_denoiser = shutil.which("dpdfnet-enhance")
        self._operation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._runner = None
        self._thread = None
        self._disposed = False
        self._sources = MainLoopSources()
        self.last_result = None
        self.last_batch_result = BatchResult()

    def _find_ffmpeg(self):
        found = shutil.which("ffmpeg")
        if found:
            return found
        for candidate in ("/usr/lib/jellyfin-ffmpeg/ffmpeg", "/opt/local/bin/ffmpeg"):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        return None

    def _begin(self):
        with self._state_lock:
            if self._disposed:
                raise MediaError(gettext.gettext("The application is closing."))
            if not self._operation_lock.acquire(blocking=False):
                raise MediaError(
                    gettext.gettext(
                        "A conversion is still running. Wait for it to finish cancelling before starting another."
                    )
                )
            self._runner = ProcessRunner()
            return self._runner

    def _end(self):
        with self._state_lock:
            self._runner = None
            self._operation_lock.release()

    @property
    def busy(self):
        return self._operation_lock.locked()

    def start_batch(self, files, settings, progress_callback, finish_callback):
        """Reserve the operation before starting its worker (no cancel race).

        ``finish_callback()`` runs on the GLib main loop; the outcome is in
        ``last_batch_result``.
        """
        snapshot = deepcopy(settings)
        files = tuple(files)
        runner = self._begin()
        try:
            self._thread = threading.Thread(
                target=self._run_batch,
                args=(files, snapshot, progress_callback, finish_callback, runner),
                name="audio-conversion",
                daemon=False,
            )
            self._thread.start()
        except Exception:
            self._end()
            raise
        return self._thread

    def _run_batch(self, files, settings, progress_callback, finish_callback, runner):
        batch = BatchResult()
        # Joined files become one output, named and tagged after the first.
        joined = files[1:] if settings.get("join_files") else ()

        def joined_progress(fraction):
            # One run covers every file; show the file it has reached.
            current = min(int(fraction * len(files)), len(files) - 1)
            progress_callback(current, files[current], fraction * len(files) - current)

        try:
            for index, source in enumerate(files[:1] if joined else files):
                if runner.cancelled.is_set():
                    batch.items.append(
                        ConversionResult(
                            source,
                            "cancelled",
                            message=gettext.gettext("Conversion cancelled."),
                        )
                    )
                    continue
                try:
                    target = self._get_output_path(
                        source,
                        settings.get("format", "mp3"),
                        settings.get("track_metadata"),
                        bool(joined),
                    )
                    if settings.get("output_directory"):
                        target = str(
                            Path(settings["output_directory"]).absolute()
                            / Path(target).name
                        )
                except (MediaError, OSError, ValueError, TypeError) as exc:
                    batch.items.append(
                        ConversionResult(
                            source,
                            "failed",
                            message=gettext.gettext(
                                "The output path is invalid. Check the source filename and destination."
                            ),
                            details=str(exc),
                        )
                    )
                    continue
                callback = (
                    None
                    if not progress_callback
                    else joined_progress
                    if joined
                    else (
                        lambda fraction, i=index, p=source: progress_callback(
                            i, p, fraction
                        )
                    )
                )
                batch.items.append(
                    self._convert_result(
                        source, target, settings, callback, runner, joined
                    )
                )
            if joined and batch.items:
                [result] = batch.items
                name = os.path.basename(result.outputs[0]) if result.outputs else ""
                batch.items += [
                    ConversionResult(
                        path,
                        result.status,
                        message=gettext.gettext("Joined into {name}").format(name=name)
                        if result.successful
                        else result.message,
                    )
                    for path in joined
                ]
        except Exception as exc:
            logger.exception("Unexpected batch failure")
            handled = len(batch.items)
            for source in files[handled:]:
                batch.items.append(
                    ConversionResult(
                        source,
                        "failed",
                        message=gettext.gettext(
                            "An unexpected error stopped this conversion."
                        ),
                        details=str(exc),
                    )
                )
        finally:
            self.last_batch_result = batch
            self._end()
        self._sources.idle(finish_callback)

    def convert_file(self, input_path, output_path, settings, progress_callback=None):
        """Convert one file synchronously; ``last_result`` holds paths and details."""
        runner = self._begin()
        try:
            self.last_result = self._convert_result(
                input_path, output_path, deepcopy(settings), progress_callback, runner
            )
            return self.last_result.successful
        finally:
            self._end()

    def _open(self, input_path, settings, runner):
        """Probe one source and plan its cuts: a list, empty for all of it."""
        source = MediaSource.resolve(input_path, settings.get("track_metadata"))
        source_identity = file_identity(source.path)
        info = probe_media(source.path, runner, self.ffmpeg_path)
        stream = audio_stream(info, source.stream_index)
        duration = audio_duration(info, stream)
        rate = int(stream["sample_rate"])
        # A file without marked segments is converted in full.
        raw = settings.get("file_markers", {}).get(input_path, [])
        # Players measure the container, which may outlast the audio;
        # audio_duration without a stream reads the container's.
        end = max(duration or 0, audio_duration(info, {}) or 0) or None
        segments = [Segment.from_mapping(s, end, rate).as_mapping() for s in raw]
        if segments and settings.get("cut_remove"):
            if end is None:
                raise MediaError(
                    gettext.gettext(
                        "The length of this file is unknown, so marked parts cannot be removed. Keep them instead."
                    )
                )
            # What lies between the marked parts, as one file.
            segments = [
                Segment.from_mapping(s, end, rate).as_mapping()
                for s in complement(segments, end)
                if round(s["stop"] * rate) > round(s["start"] * rate)
            ]
            if not segments:
                raise MediaError(
                    gettext.gettext("Every part of this file is marked for removal.")
                )
        elif settings.get("order_by_segment_number"):
            segments.sort(key=lambda s: s["segment_index"])
        else:
            segments.sort(key=lambda s: s["start"])
        return source, source_identity, info, stream, duration, segments

    def _convert_result(
        self, input_path, output_path, settings, progress_callback, runner, joined=()
    ):
        try:
            if not self.ffmpeg_path:
                raise MediaError(
                    gettext.gettext(
                        "FFmpeg is not installed. Install the FFmpeg package and try again."
                    )
                )
            settings = validate_settings(settings)
            source, source_identity, info, stream, _duration, segments = self._open(
                input_path, settings, runner
            )
            others = [self._open(path, settings, runner) for path in joined]
            rate = int(stream["sample_rate"])
            format_name = settings["format"]
            channels = settings.get("channels")
            if others:
                if format_name == "copy":
                    raise MediaError(
                        gettext.gettext(
                            "Keeping the original format cannot join files. Select an audio format."
                        )
                    )
                if channels is None:
                    # One output layout fits every file; mono joins stay mono.
                    channels = max(int(opened[3]["channels"]) for opened in others)
                    channels = max(channels, int(stream["channels"]))
                    if channels > 2:
                        raise MediaError(
                            gettext.gettext(
                                "Files with more than two channels can only be joined as mono or stereo. Choose Mono or Stereo under Channels."
                            )
                        )
            warnings, notes = [], []
            if format_name == "copy":
                effects = (
                    "noise_reduction",
                    "compressor_enabled",
                    "eq_enabled",
                    "normalize",
                )
                if (
                    any(settings.get(key) for key in effects)
                    or settings.get("volume", 1) != 1
                    or settings.get("speed", 1) != 1
                    or settings.get("channels") is not None
                    or settings.get("sample_rate") not in (None, "original")
                ):
                    raise MediaError(
                        gettext.gettext(
                            "Keeping the original format cannot apply effects, change channels or resample. Select an audio format."
                        )
                    )
                extension, muxer = copy_container(
                    stream, Path(output_path).suffix.lstrip(".")
                )
                output_path = str(Path(output_path).with_suffix("." + extension))
                params = ["-f", muxer, "-c:a", "copy"]
                filters = []
                notes.append(
                    gettext.gettext(
                        "The original format keeps the encoded audio, so cuts are approximate and effects are not applied."
                    )
                )
            else:
                extension = format_name
                params = codec_args(settings, channels, stream)
                if others:
                    # The tags come from the first file; its title and track
                    # number do not describe the whole.
                    for option in ("-metadata", "-metadata:s:a:0"):
                        params += [option, "title=", option, "track="]
                filters = build_audio_filters(
                    settings, self.noise_plugins, self.offline_denoiser
                )
                if format_name == "flac" and stream.get("codec_name", "").startswith(
                    ("pcm_f32", "pcm_f64")
                ):
                    notes.append(
                        gettext.gettext(
                            "Floating-point audio was explicitly converted to 24-bit integer PCM for FLAC; this is not a bit-exact copy."
                        )
                    )
                if format_name == "opus" and rate != 48000:
                    notes.append(
                        gettext.gettext(
                            "Opus uses a 48 kHz playback sample clock. The source sample rate cannot be represented unchanged in the output container."
                        )
                    )
                final_rate = int(params[params.index("-ar") + 1])
                if final_rate != rate:
                    notes.append(
                        gettext.gettext(
                            "The output codec requires resampling from {rate} Hz to {final_rate} Hz."
                        ).format(rate=rate, final_rate=final_rate)
                    )
                if (
                    settings.get("volume", 1) > 1
                    or (
                        settings.get("eq_enabled")
                        and any(float(g) > 0 for g in settings["eq_bands"].split(","))
                    )
                ) and not settings.get("normalize"):
                    warnings.append(
                        gettext.gettext(
                            "Raising the volume may distort. Turn on Standard volume or lower the volume."
                        )
                    )
            if extension in ("aac", "ac3", "eac3", "dts"):
                notes.append(
                    gettext.gettext(
                        "This raw audio format cannot preserve all tags or artwork."
                    )
                )
            elif extension not in ("mp3", "flac", "m4a") and any(
                s.get("disposition", {}).get("attached_pic") for s in info["streams"]
            ):
                notes.append(
                    gettext.gettext("Artwork is not preserved by this output profile.")
                )
            pieces = [
                (
                    opened[0].path,
                    opened[3]["index"],
                    cut,
                    cut["stop"] - cut["start"] if cut else opened[4] or 0,
                )
                for opened in others
                for cut in opened[5] or [None]
            ]
            processor = SegmentRenderer(self.ffmpeg_path, runner)
            speed = settings.get("speed", 1) if format_name != "copy" else 1
            with staging_directory(output_path) as directory:
                if (
                    len(segments) > 1
                    and not settings.get("cut_merge", True)
                    and not settings.get("cut_remove")
                    and not others
                ):
                    destination = Path(output_path)
                    jobs = [
                        (
                            [segment],
                            directory / f"output-{index:06d}.{extension}",
                            str(
                                destination.with_name(
                                    f"{destination.stem}_segment{index + 1}{destination.suffix}"
                                )
                            ),
                        )
                        for index, segment in enumerate(segments)
                    ]
                else:
                    jobs = [(segments, directory / f"output.{extension}", output_path)]
                staged = []
                for number, (cuts, target, destination) in enumerate(jobs):
                    processor.render(
                        source.path,
                        info,
                        stream,
                        str(target),
                        cuts,
                        filters,
                        params,
                        progress_callback
                        and (
                            lambda fraction, n=number: progress_callback(
                                (n + fraction) / len(jobs)
                            )
                        ),
                        speed,
                        directory,
                        pieces,
                    )
                    self._validate_encoding(
                        processor.last_output_info, format_name, params
                    )
                    staged.append((target, destination))
                if any(
                    before != file_identity(opened.path)
                    for opened, before, *_rest in ((source, source_identity), *others)
                ):
                    raise MediaError(
                        gettext.gettext(
                            "The source changed during conversion. No output was published; add the file again."
                        )
                    )
                outputs = publish_all(staged, runner)
            if progress_callback:
                progress_callback(1.0)
            return ConversionResult(
                input_path,
                "success",
                outputs,
                warnings=tuple(warnings),
                notes=tuple(notes),
            )
        except CancelledError:
            return ConversionResult(
                input_path,
                "cancelled",
                message=gettext.gettext(
                    "Conversion cancelled. No incomplete output was published."
                ),
            )
        except MediaError as exc:
            logger.debug("Conversion failure: %s; %s", exc, exc.details)
            return ConversionResult(
                input_path, "failed", message=str(exc), details=exc.details
            )
        except OSError as exc:
            logger.debug("File access failure", exc_info=True)
            return ConversionResult(
                input_path,
                "failed",
                message=gettext.gettext(
                    "The file or destination could not be accessed. Check permissions and free disk space."
                ),
                details=str(exc),
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return ConversionResult(
                input_path,
                "failed",
                message=gettext.gettext(
                    "The media information or conversion settings are invalid."
                ),
                details=str(exc),
            )

    @staticmethod
    def _validate_encoding(info, format_name, params):
        stream = audio_stream(info)
        if format_name == "copy":
            return
        if "-ar" in params:
            expected_rate = (
                48000 if format_name == "opus" else int(params[params.index("-ar") + 1])
            )
            if int(stream.get("sample_rate", 0)) != expected_rate:
                raise MediaError(
                    gettext.gettext(
                        "The encoder changed the requested sample rate. No output was published."
                    )
                )
        if "-ac" in params and int(stream.get("channels", 0)) != int(
            params[params.index("-ac") + 1]
        ):
            raise MediaError(
                gettext.gettext(
                    "The encoder changed the requested channel count. No output was published."
                )
            )
        if format_name == "flac" and "-bits_per_raw_sample" in params:
            expected_bits = int(params[params.index("-bits_per_raw_sample") + 1])
            if int(stream.get("bits_per_raw_sample", 0)) < expected_bits:
                raise MediaError(
                    gettext.gettext(
                        "This FFmpeg build cannot preserve the requested FLAC precision. Choose WAV or install a compatible FFmpeg package."
                    )
                )

    def _get_output_path(
        self, input_path, output_format, track_metadata=None, joined=False
    ):
        path = Path(input_path)
        entry = (track_metadata or {}).get(input_path)
        if entry and (str(input_path).startswith("bac-track:") or not path.is_file()):
            filename = Path(entry.get("output_name", "audio-track.mka")).name
            path = Path(entry["source_video"]).with_name(filename)
        extension = (
            path.suffix.lstrip(".").lower()
            if output_format == "copy"
            else output_format
        )
        if not extension or not extension.isalnum():
            raise MediaError(gettext.gettext("Select a supported output format."))
        suffix = (
            "-joined"
            if joined
            else "-copy"
            if output_format == "copy"
            else "-converted"
            if path.suffix.lower() == "." + extension
            else ""
        )
        return available_path(str(path.with_name(f"{path.stem}{suffix}.{extension}")))

    def cancel_conversion(self):
        with self._state_lock:
            if self._runner is not None:
                self._runner.cancel()

    def cleanup(self):
        self._disposed = True
        self._sources.close()
        self.cancel_conversion()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
