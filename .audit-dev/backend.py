"""Integrate transactional outputs without changing the GTK-facing API."""

from editing import ROOT, method, source_method
import backend_process
import backend_wrapper
import batch


def apply():
    path = ROOT / "app/audio/converter.py"
    text = path.read_text()
    if "from .models import" not in text:
        text = text.replace("import gettext\n", "import gettext\nimport json\n")
        text = text.replace("from gi.repository import GLib\n", "from gi.repository import GLib\n\nfrom .models import BatchResult, FileResult, MediaSource, Segment, finite_number\nfrom .process_runner import ProcessRunner, OperationCancelled\nfrom .output_transaction import OutputTransaction\nfrom .codec_profiles import build_codec_args, output_rate, ARTWORK_FORMATS, artwork_args\n")
    if "def _convert_file_staged(" not in text:
        text = text.replace("def convert_file(", "def _convert_file_staged(", 1)
    text = text.replace('if "::" in input_path:', 'if "::" in input_path and not os.path.isfile(input_path):')
    text = text.replace('            self._log_ffmpeg_version()\n', '')
    text = text.replace('cmd = [self.ffmpeg_path, "-vn", "-sn", "-y", "-i", actual_input_path]', 'cmd = [self.ffmpeg_path, "-y", "-i", actual_input_path, "-map_metadata", "0"]')
    if 'cmd.extend(artwork_args(' not in text:
        anchor = '            # Handle copy mode\n'
        text = text.replace(anchor, '            if not track_metadata:\n                cmd.extend(["-map", "0:a:0"])\n            cmd.extend(artwork_args(settings.get("_source_probe", {}), output_path, format_hint=settings.get("format")))\n\n' + anchor, 1)
    if '            process = subprocess.Popen(' in text:
        first = text.index('            process = subprocess.Popen(')
        last = text.index('            logger.info(f"Conversion successful:', first)
        text = text[:first] + '            duration = self._get_duration(actual_input_path) or 0\n            speed = float(settings.get("speed", 1.0)) if settings.get("format") != "copy" else 1.0\n            if not self._run_ffmpeg(cmd, duration / speed, progress_callback):\n                return False\n\n' + text[last:]
    text = text.replace('            # Safety check to avoid infinite loops\n            if counter > 100:\n                break\n', '')
    text = text.replace('segment_processor = SegmentProcessor(self.ffmpeg_path)', 'segment_processor = SegmentProcessor(self.ffmpeg_path, self._get_runner())\n        segment_processor.source_info = settings.get("_source_probe", {})\n        segment_processor.source_file = actual_input_path')
    text = text.replace('capture_output=True, text=True)', 'capture_output=True, text=True, timeout=10)')
    path.write_text(text)
    filters = source_method(path, "AudioConverter", "_build_audio_filters")
    if 'finite_number(settings.get("speed"' not in filters:
        filters = filters.replace('    filters = []\n', '''    settings = dict(settings)
    settings["speed"] = finite_number(settings.get("speed", 1.0), "Speed", 0.1, 100)
    settings["volume"] = finite_number(settings.get("volume", 1.0), "Volume", 0, 10)
    for key in ("gate_intensity", "compressor_intensity", "noise_strength", "noise_speech_strength", "noise_voice_enhance"):
        if key in settings:
            settings[key] = finite_number(settings[key], key, 0, 1)
    if settings.get("noise_reduction") and not self.gtcrn_ladspa_path:
        raise ValueError("Neural noise reduction is unavailable: install the GTCRN plugin and models")
    if settings.get("transient_enabled") and not self.gtcrn_ladspa_path:
        raise ValueError("Transient suppression is unavailable: install the audio processing plugin")
    filters = []
''', 1)
        filters = filters.replace('while remaining > 100.0:', 'while remaining > 2.0:').replace('filters.append("atempo=100.0")', 'filters.append("atempo=2.0")').replace('remaining /= 100.0', 'remaining /= 2.0')
        method(path, "AudioConverter", "_build_audio_filters", filters)
    backend_process.apply()
    backend_wrapper.apply()
    batch.apply()
    profiles = ROOT / "app/audio/codec_profiles.py"
    if 'def artwork_args(' not in profiles.read_text():
        with profiles.open('a') as stream:
            stream.write('''\n\ndef artwork_args(info, output_path, input_index=0, format_hint=None):
    """Map only an attached picture, never a video's moving-image stream."""
    from pathlib import Path
    fmt = Path(output_path).suffix.lstrip(".").lower() if format_hint in (None, "copy") else format_hint
    if fmt not in ARTWORK_FORMATS:
        return []
    for image in info.get("streams", []):
        if image.get("disposition", {}).get("attached_pic") and image.get("codec_name") in ("png", "mjpeg"):
            return ["-map", f"{input_index}:{image['index']}", "-c:v", "copy", "-disposition:v:0", "attached_pic"]
    return []
''')
