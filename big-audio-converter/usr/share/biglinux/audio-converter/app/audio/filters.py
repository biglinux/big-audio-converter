"""Validated filter policy shared by preview and export."""

import math
import logging
import os
from pathlib import Path
from .models import finite_number

logger = logging.getLogger(__name__)

def build_audio_filters(settings, ladspa_path=None):
    """Build the list of FFmpeg audio filters from settings.

    Filter order: HPF → Transient → Compressor → GTCRN NR → Gate → EQ → Volume → Speed → Normalize
    """
    settings = dict(settings)
    settings["speed"] = finite_number(settings.get("speed", 1.0), "Speed", 0.1, 100)
    settings["volume"] = finite_number(settings.get("volume", 1.0), "Volume", 0, 10)
    for key in ("gate_intensity", "compressor_intensity", "noise_strength", "noise_speech_strength", "noise_voice_enhance"):
        if key in settings:
            settings[key] = finite_number(settings[key], key, 0, 1)
    if settings.get("noise_reduction") and not ladspa_path:
        raise ValueError("Neural noise reduction is unavailable: install the GTCRN plugin and models")
    if settings.get("transient_enabled") and not ladspa_path:
        raise ValueError("Transient suppression is unavailable: install the audio processing plugin")
    filters = []

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

    # 8. Speed (atempo supports 0.5-100.0, chain filters for values outside)
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

    return filters
