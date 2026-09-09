"""Explicit codec, sample-rate and PCM-precision policies."""

import re
from .models import finite_number

FORMATS = {
    "mp3": ("mp3", "libmp3lame"), "ogg": ("ogg", "libvorbis"),
    "flac": ("flac", "flac"), "wav": ("wav", "pcm_s16le"),
    "aac": ("adts", "aac"), "opus": ("opus", "libopus"),
}
COPY_MUXERS = {"aac": "adts", "m4a": "ipod", "wma": "asf", "aif": "aiff", "mka": "matroska"}
ARTWORK_FORMATS = {"mp3", "flac", "m4a", "ipod"}


def output_rate(settings, source):
    requested = settings.get("sample_rate")
    rate = int(finite_number(requested or source.get("sample_rate", 48000), "Sample rate", 8000, 768000))
    allowed = {
        "opus": (8000, 12000, 16000, 24000, 48000),
        "mp3": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
        "aac": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000),
    }.get(settings.get("format", "mp3"))
    if allowed is not None and rate not in allowed:
        if requested:
            raise ValueError("The selected sample rate is not supported by this codec")
        rate = min(allowed, key=lambda candidate: abs(candidate - rate))
    return rate


def pcm_codec(source):
    codec = source.get("codec_name", "")
    if codec in {"pcm_f32le", "pcm_f32be"}:
        return "pcm_f32le"
    if codec in {"pcm_f64le", "pcm_f64be"}:
        return "pcm_f64le"
    bits = int(source.get("bits_per_raw_sample") or source.get("bits_per_sample") or 0)
    if bits:
        return "pcm_s32le" if bits > 24 else "pcm_s24le" if bits > 16 else "pcm_s16le"
    sample_format = source.get("sample_fmt", "s16")
    if sample_format.startswith("dbl"):
        return "pcm_f64le"
    if sample_format.startswith("flt"):
        return "pcm_f32le"
    return "pcm_s32le" if sample_format.startswith("s32") else "pcm_s16le"


def build_codec_args(settings, channels=None, source=None):
    channels = settings.get("channels") if channels is None else channels
    fmt = settings.get("format", "mp3")
    if fmt == "copy":
        return ["-c:a", "copy"]
    if fmt not in FORMATS:
        raise ValueError("Unsupported output format")
    muxer, codec = FORMATS[fmt]
    if fmt == "wav":
        codec = pcm_codec(source or {})
    args = ["-f", muxer, "-c:a", codec]
    bitrate = settings.get("bitrate")
    if bitrate and fmt in ("mp3", "aac", "ogg", "opus"):
        if not isinstance(bitrate, str) or not re.fullmatch(r"[0-9]{1,4}k", bitrate):
            raise ValueError("Bitrate must use kilobits per second, for example 192k")
        maximum = 320 if fmt == "mp3" else 510 if fmt == "opus" else 1536
        finite_number(bitrate[:-1], "Bitrate", 6 if fmt == "opus" else 8, maximum)
        args.extend(["-b:a", bitrate])
    if channels:
        if isinstance(channels, bool) or channels not in (1, 2):
            raise ValueError("Channels must be Original, Mono or Stereo")
        args.extend(["-ac", str(channels)])
    if source is not None:
        args.extend(["-ar", str(output_rate(settings, source))])
        if fmt == "mp3" and not channels and int(source.get("channels", 2)) > 2:
            raise ValueError("MP3 supports at most two channels; select Mono or Stereo explicitly")
    return args


def artwork_args(info, output_path, input_index=0, format_hint=None):
    """Map only an attached picture, never a video's moving-image stream."""
    from pathlib import Path
    fmt = Path(output_path).suffix.lstrip(".").lower() if format_hint in (None, "copy") else format_hint
    if fmt not in ARTWORK_FORMATS:
        return []
    for image in info.get("streams", []):
        if image.get("disposition", {}).get("attached_pic") and image.get("codec_name") in ("png", "mjpeg"):
            return ["-map", f"{input_index}:{image['index']}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    return []
