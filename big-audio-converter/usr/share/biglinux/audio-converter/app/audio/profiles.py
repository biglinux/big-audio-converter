"""Validated processing profiles shared by conversion and preview."""

import gettext
import math
from pathlib import Path

from .media import pcm_codec
from .process import MediaError

FORMATS = {
    "mp3": ("mp3", "libmp3lame"),
    "flac": ("flac", "flac"),
    "ogg": ("ogg", "libvorbis"),
    "wav": ("wav", "pcm_s16le"),
    "aac": ("adts", "aac"),
    "opus": ("opus", "libopus"),
}
BITRATES = {
    "mp3": ("32k", "64k", "96k", "128k", "160k", "192k", "256k", "320k"),
    "aac": ("32k", "64k", "96k", "128k", "160k", "192k", "256k", "320k"),
    "ogg": ("64k", "96k", "128k", "160k", "192k", "256k", "320k"),
    "opus": ("32k", "64k", "96k", "128k", "160k", "192k", "256k", "320k"),
}
SAMPLE_RATES = {
            "mp3": (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000),
            "opus": (8000, 12000, 16000, 24000, 48000),
            "aac": (7350, 8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000),
        }

COPY_CONTAINERS = {
    "mp3": ("mp3", "mp3"), "aac": ("m4a", "ipod"),
    "alac": ("m4a", "ipod"), "flac": ("flac", "flac"),
    "vorbis": ("ogg", "ogg"), "opus": ("opus", "opus"),
    "wmav1": ("wma", "asf"), "wmav2": ("wma", "asf"),
    "ac3": ("ac3", "ac3"), "eac3": ("eac3", "eac3"), "dts": ("dts", "dts"),
}
NUMERIC_LIMITS = {
    "volume": (0, 10), "speed": (0.1, 5),
    "hpf_frequency": (20, 20000), "transient_attack": (-1, 1),
    "compressor_intensity": (0, 1), "gate_intensity": (0, 1),
    "noise_strength": (0, 1), "noise_model": (0, 1),
    "noise_speech_strength": (0, 1), "noise_lookahead": (0, 200),
    "noise_voice_enhance": (0, 1),
}
BOOLEAN_KEYS = (
    "noise_reduction", "transient_enabled", "hpf_enabled", "gate_enabled",
    "compressor_enabled", "eq_enabled", "normalize", "prevent_clipping", "noise_model_blend", "allow_precision_reduction",
)


def validate_settings(settings):
    result = dict(settings)
    for key, (low, high) in NUMERIC_LIMITS.items():
        if key not in result:
            continue
        try:
            value = float(result[key])
        except (ValueError, TypeError, OverflowError) as exc:
            raise MediaError(gettext.gettext('Invalid value for {key}.').format(key=key)) from exc
        if not math.isfinite(value) or not low <= value <= high:
            raise MediaError(gettext.gettext('{key} must be between {low} and {high}.').format(key=key, low=low, high=high))
        result[key] = int(value) if key in ("noise_model", "noise_lookahead", "hpf_frequency") else value
    for key in BOOLEAN_KEYS:
        if key in result and not isinstance(result[key], bool):
            raise MediaError(gettext.gettext('Invalid switch value for {key}.').format(key=key))
    if result.get("eq_enabled"):
        try:
            gains = [float(value) for value in result.get("eq_bands", "0," * 9 + "0").split(",")]
        except (ValueError, AttributeError, TypeError) as exc:
            raise MediaError(gettext.gettext('The equalizer bands are invalid.')) from exc
        if len(gains) != 10 or any(not math.isfinite(g) or not -40 <= g <= 40 for g in gains):
            raise MediaError(gettext.gettext('The equalizer needs ten finite gains between -40 and 40 dB.'))
    return result


def codec_args(settings, channels=None, stream=None):
    format_name = settings["format"]
    if format_name == "copy":
        return ["-c:a", "copy"]
    if format_name not in FORMATS:
        raise MediaError(gettext.gettext('Select a supported output format.'))
    muxer, codec = FORMATS[format_name]
    if format_name == "wav" and stream is not None:
        codec = pcm_codec(stream)
    args = ["-f", muxer, "-c:a", codec]
    if format_name == "flac" and stream:
        source_codec = stream.get("codec_name", "")
        sample_format = stream.get("sample_fmt", "")
        bits = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0)
        floating_pcm = source_codec.startswith(("pcm_f32", "pcm_f64"))
        if floating_pcm or bits > 32:
            if not settings.get("allow_precision_reduction"):
                raise MediaError(gettext.gettext('FLAC cannot preserve this PCM representation. Choose WAV or explicitly allow conversion to 24-bit PCM in Advanced encoding.'))
            bits = 24
        elif not bits:
            bits = 24 if sample_format.startswith(("flt", "dbl")) else {"u8": 8, "s16": 16, "s32": 32}.get(sample_format.rstrip("p"), 16)
        args += ["-sample_fmt", "s16" if bits <= 16 else "s32", "-bits_per_raw_sample", str(bits)]
        if bits > 24:
            # FFmpeg 7.x requires explicit opt-in to its 32-bit FLAC encoder.
            # Verify the encoded precision before publishing, including on newer builds.
            args += ["-strict", "experimental"]
    bitrate = settings.get("bitrate")
    if bitrate and format_name in BITRATES:
        if bitrate not in BITRATES[format_name]:
            raise MediaError(gettext.gettext('The selected bitrate is not supported by this output profile.'))
        args += ["-b:a", bitrate]
    if channels is not None:
        if channels not in (1, 2) or isinstance(channels, bool):
            raise MediaError(gettext.gettext('Select original, mono or stereo channels.'))
        args += ["-ac", str(channels)]
    if stream:
        source_channels = int(stream.get("channels", 0))
        if format_name == "mp3" and not channels and source_channels > 2:
            raise MediaError(gettext.gettext('MP3 supports mono or stereo. Select stereo channels or another format.'))
        source_rate = int(stream["sample_rate"])
        requested = settings.get("sample_rate")
        rate = source_rate if requested in (None, "original") else int(requested)
        if rate <= 0 or rate > 768000:
            raise MediaError(gettext.gettext('Select a valid output sample rate.'))
        # Preserve the source rate unless the codec cannot represent it.
        supported = SAMPLE_RATES.get(format_name)
        if supported and rate not in supported:
            if requested not in (None, "original"):
                raise MediaError(gettext.gettext('The selected codec does not support that sample rate.'))
            rate = min(supported, key=lambda supported_rate: abs(supported_rate - rate))
        args += ["-ar", str(rate)]
    return args


def copy_container(stream, requested_extension):
    codec = stream["codec_name"]
    if codec.startswith("pcm_") and requested_extension in ("wav", "aiff", "aif", "caf", "nut", "mka"):
        return requested_extension, {"aif": "aiff", "mka": "matroska"}.get(requested_extension, requested_extension)
    if codec == "aac" and requested_extension == "aac":
        return "aac", "adts"
    if codec in COPY_CONTAINERS:
        return COPY_CONTAINERS[codec]
    # Matroska audio preserves many codecs not supported by the common
    # audio-only containers. FFmpeg still validates the actual combination.
    return "mka", "matroska"


def metadata_args(info, stream, format_name, input_index=0):
    args = ["-map", f"{input_index}:{stream['index']}", "-map_metadata", str(input_index),
            "-map_metadata:s:a:0", f"{input_index}:s:{stream['index']}"]
    if format_name in ("mp3", "flac", "m4a"):
        for picture in info["streams"]:
            if picture.get("disposition", {}).get("attached_pic") and picture.get("codec_name") in ("mjpeg", "png"):
                args += ["-map", f"{input_index}:{picture['index']}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
                break
    return args


def build_audio_filters(settings, ladspa_path=None):
    """Build the list of FFmpeg audio filters from settings.

    Filter order: HPF → Transient → Compressor → GTCRN NR → Gate → EQ → Volume → Speed → Normalize
    """
    settings = validate_settings(settings)
    filters = []

    if settings.get("noise_reduction") and not ladspa_path:
        raise MediaError(gettext.gettext('Noise reduction is unavailable. Install the GTCRN plugin or turn this effect off.'))
    if settings.get("transient_enabled") and not ladspa_path:
        raise MediaError(gettext.gettext('Transient suppression is unavailable. Install its plugin or turn this effect off.'))

    # 1. High-pass filter (remove low-frequency rumble)
    if settings.get("hpf_enabled", False):
        freq = settings.get("hpf_frequency", 80)
        filters.append(f"highpass=f={freq}:poles=2")

    # 2. Transient suppressor (clicks and plosives)
    if settings.get("transient_enabled", False) and ladspa_path:
        attack = settings.get("transient_attack", -0.5)
        ladspa_dir = str(Path(ladspa_path).parent)
        filters.append(
            f"ladspa=file={ladspa_dir}/transient_split.so:plugin=transient:controls=c0={attack}"
        )

    # 3. Compressor (before NR to even out dynamics)
    if settings.get("compressor_enabled", False):
        ci = settings.get("compressor_intensity", 1.0)
        threshold_db = -20.0 - ci * 20.0
        ratio = 3.0 + ci * 7.0
        makeup_db = 6.0 + ci * 12.0
        knee_db = 12.0 + ci * 4.0
        threshold_lin = 10 ** (threshold_db / 20.0)
        makeup_lin = 10 ** (makeup_db / 20.0)
        knee_lin = 10 ** (knee_db / 20.0)  # FFmpeg acompressor knee range: 1-8
        filters.append(
            f"acompressor=threshold={threshold_lin:.6f}:ratio={ratio:.1f}:attack=150:release=800:makeup={makeup_lin:.4f}:knee={knee_lin:.4f}:detection=rms"
        )

    # 4. GTCRN noise reduction
    if settings.get("noise_reduction", False) and ladspa_path:
        strength = settings.get("noise_strength", 1.0)
        model = settings.get("noise_model", 0)
        speech_strength = settings.get("noise_speech_strength", 1.0)
        lookahead = settings.get("noise_lookahead", 0)
        voice_enhance = settings.get("noise_voice_enhance", 0.0)
        model_blend = 1 if settings.get("noise_model_blend", False) else 0
        filters.append(
            f"ladspa=file={ladspa_path}:plugin=gtcrn_mono:controls="
            f"c0=1|c1={strength}|c2={model}|c3={speech_strength}|c4={lookahead}|c5={voice_enhance}|c6={model_blend}"
        )

    # 5. Noise gate (after NR)
    if settings.get("gate_enabled", False):
        intensity = settings.get("gate_intensity", 0.5)
        threshold_db = -50.0 + math.sqrt(intensity) * 35.0
        range_db = -40.0 - math.sqrt(intensity) * 50.0
        threshold_lin = 10 ** (threshold_db / 20.0)
        range_lin = 10 ** (range_db / 20.0)
        filters.append(
            f"agate=threshold={threshold_lin:.6f}:range={range_lin:.6f}:attack=10:release=10:detection=rms"
        )

    # 6. Equalizer
    if settings.get("eq_enabled", False):
        eq_bands_str = settings.get("eq_bands", "0,0,0,0,0,0,0,0,0,0")
        try:
            gains = [float(x) for x in eq_bands_str.split(",")]
        except (ValueError, AttributeError):
            gains = [0.0] * 10
        eq_freqs = [31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000]
        for i, (freq, gain) in enumerate(zip(eq_freqs, gains)):
            if gain != 0.0:
                filters.append(f"equalizer=f={freq}:width_type=o:w=1.5:g={gain}")

    # 7. Volume
    if settings.get("volume", 1.0) != 1.0:
        filters.append(f"volume={settings['volume']}")

    # 8. Keep every atempo stage at or below 2 to avoid sample skipping.
    speed = settings.get("speed", 1.0)
    if speed != 1.0:
        remaining = speed
        while remaining < 0.5:
            filters.append("atempo=0.5")
            remaining /= 0.5
        while remaining > 2.0:
            filters.append("atempo=2.0")
            remaining /= 2.0
        filters.append(f"atempo={remaining}")

    # 9. Normalization (last)
    if settings.get("normalize", False):
        filters.append("loudnorm=I=-16:LRA=11:TP=-1.5")

    if settings.get("prevent_clipping", False):
        filters.append("alimiter=limit=0.891251:level=false:latency=true")
    return filters

