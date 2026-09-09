"""Audio conversion with explicit jobs, validated plans and safe output files."""

import gettext
import logging
import os
import shutil
import threading
import time
from copy import deepcopy
from pathlib import Path

from .media import (
    BatchResult,
    ConversionResult,
    MediaSource,
    Segment,
    audio_duration,
    audio_stream,
    probe_media,
)
from .output import available_path, publish_all, staging_directory
from .process import CancelledError, MediaError, ProcessRunner
from .profiles import (
    build_audio_filters,
    codec_args,
    copy_container,
    metadata_args,
    validate_settings,
)
from .segment_processor import SegmentProcessor, command_error

logger = logging.getLogger(__name__)


def _dispatch_once(callback, *args):
    """Compatibility boundary for the existing GTK batch callback API."""
    try:
        from gi.repository import GLib
    except ImportError:
        callback(*args)
        return

    def deliver():
        callback(*args)
        return False

    GLib.idle_add(deliver)


class AudioConverter:
    def __init__(self, gtcrn_ladspa_path=None):
        self.ffmpeg_path = self._find_ffmpeg()
        self.gtcrn_ladspa_path = gtcrn_ladspa_path if gtcrn_ladspa_path and os.path.isfile(gtcrn_ladspa_path) else None
        self._operation_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._runner = None
        self._thread = None
        self._disposed = False
        self.cancel_flag = False
        self.last_result = None
        self.last_batch_result = BatchResult()
        self.current_process = None  # Kept for callers of the original API.

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
                raise MediaError(gettext.gettext('The application is closing.'))
            if not self._operation_lock.acquire(blocking=False):
                raise MediaError(gettext.gettext('A conversion is still running. Wait for it to finish cancelling before starting another.'))
            self.cancel_flag = False
            self._runner = ProcessRunner()
            return self._runner

    def _end(self):
        with self._state_lock:
            self._runner = None
            self.current_process = None
            self._operation_lock.release()

    @property
    def busy(self):
        return self._operation_lock.locked()

    def start_batch(self, files, settings, progress_callback, finish_callback):
        """Reserve the operation before starting its worker (no cancel race)."""
        runner = self._begin()
        snapshot = deepcopy(settings)
        self._thread = threading.Thread(
            target=self._run_batch, args=(tuple(files), snapshot, progress_callback, finish_callback, runner),
            name="audio-conversion", daemon=False,
        )
        self._thread.start()
        return self._thread

    def convert_all_files(self, files, settings, progress_callback, finish_callback):
        """Synchronous worker API; GUI code should call ``start_batch``."""
        runner = self._begin()
        self._run_batch(tuple(files), deepcopy(settings), progress_callback, finish_callback, runner)

    def _run_batch(self, files, settings, progress_callback, finish_callback, runner):
        batch = BatchResult()
        try:
            for index, source in enumerate(files):
                if runner.cancelled.is_set():
                    batch.items.append(ConversionResult(source, "cancelled", message=gettext.gettext('Conversion cancelled.')))
                    continue
                try:
                    target = self._get_output_path(source, settings.get("format", "mp3"), settings.get("track_metadata"))
                    if settings.get("output_directory"):
                        target = str(Path(settings["output_directory"]).absolute() / Path(target).name)
                except (MediaError, OSError, ValueError, TypeError) as exc:
                    batch.items.append(ConversionResult(source, "failed", message=gettext.gettext('The output path is invalid. Check the source filename and destination.'), details=str(exc)))
                    continue
                callback = (lambda fraction, i=index, p=source: progress_callback(i, p, fraction)) if progress_callback else None
                batch.items.append(self._convert_result(source, target, settings, callback, runner))
        except Exception as exc:
            logger.exception("Unexpected batch failure")
            handled = len(batch.items)
            for source in files[handled:]:
                batch.items.append(ConversionResult(source, "failed", message=gettext.gettext('An unexpected error stopped this conversion.'), details=str(exc)))
        finally:
            self.last_batch_result = batch
            self._end()
        successful = batch.successful_sources
        cancelled = any(item.status == "cancelled" for item in batch.items)
        message = "Conversion cancelled." if cancelled else "Conversion finished."
        if not self._disposed:
            _dispatch_once(finish_callback, bool(successful), message, successful)

    def convert_file(self, input_path, output_path, settings, progress_callback=None):
        """Convert one file; read ``last_result`` for actual paths and details."""
        runner = self._begin()
        try:
            self.last_result = self._convert_result(input_path, output_path, deepcopy(settings), progress_callback, runner)
            return self.last_result.successful
        finally:
            self._end()

    def _convert_result(self, input_path, output_path, settings, progress_callback, runner):
        try:
            if not self.ffmpeg_path:
                raise MediaError(gettext.gettext('FFmpeg is not installed. Install the FFmpeg package and try again.'))
            settings = validate_settings(settings)
            source = MediaSource.resolve(input_path, settings.get("track_metadata"))
            source_stat = os.stat(source.path)
            info = probe_media(source.path, runner, self.ffmpeg_path)
            stream = audio_stream(info, source.stream_index)
            duration = audio_duration(info, stream)
            rate = int(stream["sample_rate"])
            format_name = settings["format"]
            warnings = []
            if format_name == "copy":
                effects = ("noise_reduction", "transient_enabled", "hpf_enabled", "gate_enabled", "compressor_enabled", "eq_enabled", "normalize", "prevent_clipping")
                if any(settings.get(key) for key in effects) or settings.get("volume", 1) != 1 or settings.get("speed", 1) != 1 or settings.get("channels") is not None or settings.get("sample_rate") not in (None, "original"):
                    raise MediaError(gettext.gettext('Fast Copy cannot apply effects, change channels or resample. Select an encoding format.'))
                extension, muxer = copy_container(stream, Path(output_path).suffix.lstrip("."))
                output_path = str(Path(output_path).with_suffix("." + extension))
                params = ["-f", muxer, "-c:a", "copy"]
                filters = []
                warnings.append(gettext.gettext('Fast Copy preserves encoded audio packets; cut boundaries are approximate. Effects are not applied.'))
            else:
                extension = format_name
                params = codec_args(settings, settings.get("channels"), stream)
                for effect, plugin in (("noise_reduction", self.gtcrn_ladspa_path),
                                       ("transient_enabled", str(Path(self.gtcrn_ladspa_path).parent / "transient_split.so") if self.gtcrn_ladspa_path else None)):
                    if settings.get(effect) and (not plugin or not os.path.isfile(plugin)):
                        raise MediaError(gettext.gettext('A selected audio plugin is unavailable. Install it or turn that effect off.'))
                filters = self._build_audio_filters(settings)
                if format_name == "flac" and stream.get("codec_name", "").startswith(("pcm_f32", "pcm_f64")):
                    warnings.append(gettext.gettext('Floating-point audio was explicitly converted to 24-bit integer PCM for FLAC; this is not a bit-exact copy.'))
                if format_name == "opus" and rate != 48000:
                    warnings.append(gettext.gettext('Opus uses a 48 kHz playback sample clock. The source sample rate cannot be represented unchanged in the output container.'))
                final_rate = int(params[params.index("-ar") + 1])
                if final_rate != rate:
                    warnings.append(gettext.gettext('The output codec requires resampling from {rate} Hz to {final_rate} Hz.').format(rate=rate, final_rate=final_rate))
                if (settings.get("volume", 1) > 1 or (settings.get("eq_enabled") and any(float(g) > 0 for g in settings["eq_bands"].split(",")))) and (not settings.get("prevent_clipping") and not settings.get("normalize")):
                    warnings.append(gettext.gettext('Positive gain may clip. Enable clipping protection or reduce the gain.'))
            if extension in ("aac", "ac3", "eac3", "dts"):
                warnings.append(gettext.gettext('This raw audio format cannot preserve all tags or artwork.'))
            elif extension not in ("mp3", "flac", "m4a") and any(s.get("disposition", {}).get("attached_pic") for s in info["streams"]):
                warnings.append(gettext.gettext('Artwork is not preserved by this output profile.'))
            segments = []
            if settings.get("cut_enabled"):
                raw = settings.get("file_markers", {}).get(input_path, settings.get("cut_segments", []))
                segments = [Segment.from_mapping(s, duration, rate).as_mapping() for s in raw]
                if settings.get("order_by_segment_number"):
                    segments.sort(key=lambda s: s["segment_index"])
                else:
                    segments.sort(key=lambda s: s["start"])
            processor = SegmentProcessor(self.ffmpeg_path, runner)
            with staging_directory(output_path) as directory:
                staged = []
                filter_graph = ",".join(filters) or None
                if segments and not settings.get("cut_merge", True) and len(segments) > 1:
                    destination = Path(output_path)
                    for index, segment in enumerate(segments):
                        runner.check_cancelled()
                        target = directory / f"output-{index:06d}.{extension}"
                        processor._extract_segment(source.path, segment, str(target), filter_graph, stream["index"], params, info)
                        self._validate_encoding(processor.last_output_info, format_name, params)
                        staged.append((target, str(destination.with_name(f"{destination.stem}_segment{index + 1}{destination.suffix}"))))
                        if progress_callback:
                            progress_callback((index + 1) / len(segments) * 0.95)
                elif segments:
                    target = directory / f"output.{extension}"
                    # Copy has no codec parameters for the editing strategy;
                    # the safe audio-only extension supplies its muxer.
                    processor.process_segments(source.path, segments, extension, str(directory), filter_graph,
                                               stream["index"], str(target), None if format_name == "copy" else params)
                    self._validate_encoding(processor.last_output_info, format_name, params)
                    staged.append((target, output_path))
                else:
                    target = directory / f"output.{extension}"
                    command = [self.ffmpeg_path, "-hide_banner", "-nostdin", "-n", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", source.path]
                    command += metadata_args(info, stream, extension)
                    if filter_graph:
                        command += ["-af", filter_graph]
                    command += params + ["-nostats", "-progress", "pipe:1", str(target)]
                    progress = self._progress_consumer(progress_callback, duration, settings.get("speed", 1) if format_name != "copy" else 1)
                    result = runner.run(command, stdout_callback=progress)
                    if result.returncode:
                        raise command_error(result.stderr)
                    processor.validate_output(str(target))
                    self._validate_encoding(processor.last_output_info, format_name, params)
                    staged.append((target, output_path))
                current_stat = os.stat(source.path)
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
                if any(getattr(source_stat, field) != getattr(current_stat, field) for field in fields):
                    raise MediaError(gettext.gettext('The source changed during conversion. No output was published; add the file again.'))
                outputs = publish_all(staged, runner)
            if progress_callback:
                progress_callback(1.0)
            return ConversionResult(input_path, "success", outputs, warnings=tuple(warnings))
        except CancelledError:
            return ConversionResult(input_path, "cancelled", message=gettext.gettext('Conversion cancelled. No incomplete output was published.'))
        except MediaError as exc:
            logger.debug("Conversion failure: %s; %s", exc, exc.details)
            return ConversionResult(input_path, "failed", message=str(exc), details=exc.details)
        except OSError as exc:
            logger.debug("File access failure", exc_info=True)
            return ConversionResult(input_path, "failed", message=gettext.gettext('The file or destination could not be accessed. Check permissions and free disk space.'), details=str(exc))
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            return ConversionResult(input_path, "failed", message=gettext.gettext('The media information or conversion settings are invalid.'), details=str(exc))

    @staticmethod
    def _validate_encoding(info, format_name, params):
        stream = audio_stream(info)
        if format_name == "copy":
            return
        if "-ar" in params:
            expected_rate = 48000 if format_name == "opus" else int(params[params.index("-ar") + 1])
            if int(stream.get("sample_rate", 0)) != expected_rate:
                raise MediaError(gettext.gettext('The encoder changed the requested sample rate. No output was published.'))
        if "-ac" in params and int(stream.get("channels", 0)) != int(params[params.index("-ac") + 1]):
            raise MediaError(gettext.gettext('The encoder changed the requested channel count. No output was published.'))
        if format_name == "flac" and "-bits_per_raw_sample" in params:
            expected_bits = int(params[params.index("-bits_per_raw_sample") + 1])
            if int(stream.get("bits_per_raw_sample", 0)) < expected_bits:
                raise MediaError(gettext.gettext('This FFmpeg build cannot preserve the requested FLAC precision. Choose WAV or install a compatible FFmpeg package.'))

    @staticmethod
    def _progress_consumer(callback, duration, speed):
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
                        fraction = float(line.split(b"=", 1)[1]) / 1_000_000 * speed / duration
                    except ValueError:
                        continue
                    now = time.monotonic()
                    if now - last_update >= 0.1:
                        callback(max(0.0, min(0.99, fraction)))
                        last_update = now
            if len(pending) > 65536:
                pending.clear()
        return consume

    def _build_audio_filters(self, settings):
        return build_audio_filters(settings, self.gtcrn_ladspa_path)

    def _build_codec_args(self, settings, channels=None):
        return codec_args(settings, channels)

    def _get_output_path(self, input_path, output_format, track_metadata=None):
        path = Path(input_path)
        entry = (track_metadata or {}).get(input_path)
        if entry and (str(input_path).startswith("bac-track:") or not path.is_file()):
            filename = Path(entry.get("output_name", "audio-track.mka")).name
            path = Path(entry["source_video"]).with_name(filename)
        elif "::" in str(input_path) and not path.is_file():
            video, track = str(input_path).split("::", 1)
            path = Path(video).with_name(f"{Path(video).stem}-{Path(track).name}")
        extension = path.suffix.lstrip(".").lower() if output_format == "copy" else output_format
        if not extension or not extension.isalnum():
            raise MediaError(gettext.gettext('Select a supported output format.'))
        suffix = "-copy" if output_format == "copy" else "-converted" if path.suffix.lower() == "." + extension else ""
        return available_path(str(path.with_name(f"{path.stem}{suffix}.{extension}")))

    def _get_duration(self, file_path):
        info = probe_media(file_path, ffmpeg_path=self.ffmpeg_path)
        return audio_duration(info, audio_stream(info)) or 0

    def get_file_metadata(self, file_path):
        info = probe_media(file_path, ffmpeg_path=self.ffmpeg_path)
        stream = audio_stream(info)
        duration = audio_duration(info, stream)
        size = os.path.getsize(file_path)
        result = {"size": f"{size / (1024 * 1024):.1f} MB" if size >= 1024 * 1024 else f"{size / 1024:.1f} KB",
                  "format": Path(file_path).suffix.lstrip(".").upper()}
        if duration:
            result["duration"] = f"{int(duration // 60)}:{int(duration % 60):02d}"
        bitrate = stream.get("bit_rate")
        if bitrate and str(bitrate).isdigit():
            result["bitrate"] = f"{int(bitrate) // 1000} kbps"
        return result

    def cancel_conversion(self):
        self.cancel_flag = True
        with self._state_lock:
            if self._runner is not None:
                self._runner.cancel()

    def cleanup(self):
        self._disposed = True
        self.cancel_conversion()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
