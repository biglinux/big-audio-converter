"""Bounded waveform decoding and compatibility entry points for the UI."""

import os
from .envelope import PeakEnvelope
from .media_probe import audio_stream, media_duration, probe_media
from .models import MediaSource
from .process_runner import ProcessRunner


def decode_envelope(source, ffmpeg="ffmpeg", runner=None, capacity=262144):
    """Decode full-band audio in small blocks and retain only channel-safe peaks."""
    runner = runner if runner is not None else ProcessRunner()
    info = probe_media(source.path, ffmpeg, runner)
    stream = audio_stream(info, source.stream_index)
    envelope = PeakEnvelope(int(stream["sample_rate"]), int(stream["channels"]),
                            media_duration(info, stream), capacity)
    result = runner.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-v", "error", "-xerror",
         "-protocol_whitelist", "file,pipe", "-i", os.path.abspath(source.path),
         "-map", f"0:{stream['index']}", "-vn", "-sn", "-dn",
         "-c:a", "pcm_f32le", "-f", "f32le", "pipe:1"],
        stdout_consumer=envelope.feed, timeout=600,
    )
    if result.returncode:
        raise ValueError("Audio decoding failed while generating the waveform")
    data, rate, duration = envelope.finish()
    if not len(data) or duration <= 0:
        raise ValueError("The selected stream contains no decoded audio")
    return data, rate, duration


def request(file_path, converter_instance, visualizer, file_markers=None,
            zoom_control_box=None, track_metadata=None, *, enabled=True):
    """Queue the latest request on the visualizer's single worker."""
    from .waveform_jobs import WaveformJobs
    service = getattr(visualizer, "_waveform_jobs", None)
    if service is None or service.closed:
        service = WaveformJobs(visualizer)
        visualizer._waveform_jobs = service
    service.request(file_path, converter_instance.ffmpeg_path, file_markers,
                    zoom_control_box, track_metadata, enabled)


def generate(file_path, converter_instance, visualizer, file_markers=None,
             zoom_control_box=None, track_metadata=None):
    return request(file_path, converter_instance, visualizer, file_markers,
                   zoom_control_box, track_metadata, enabled=True)


def activate_without_waveform(file_path, converter_instance, visualizer,
                              file_markers=None, zoom_control_box=None,
                              track_metadata=None):
    return request(file_path, converter_instance, visualizer, file_markers,
                   zoom_control_box, track_metadata, enabled=False)


def cancel(visualizer):
    service = getattr(visualizer, "_waveform_jobs", None)
    if service is not None:
        service.cancel()


def shutdown():
    from .waveform_jobs import close_all
    close_all()
