"""Use one validated DSP policy for preview and export."""

from editing import ROOT, method, source_method, write


def apply():
    converter = ROOT / "app/audio/converter.py"
    filters_path = ROOT / "app/audio/filters.py"
    if not filters_path.exists():
        source = source_method(converter, "AudioConverter", "_build_audio_filters")
        source = source.replace('def _build_audio_filters(self, settings):', 'def build_audio_filters(settings, ladspa_path=None):')
        source = source.replace('self.gtcrn_ladspa_path', 'ladspa_path')
        if 'self.' in source:
            raise RuntimeError("The DSP builder still depends on its converter instance")
        source = source.replace('gains = [float(x) for x in bands.split(",")]', 'gains = [finite_number(x, "Equalizer gain", -40, 40) for x in bands.split(",")]')
        write(filters_path, '"""Validated filter policy shared by preview and export."""\n\nimport math\nimport logging\nimport os\nfrom pathlib import Path\nfrom .models import finite_number\n\nlogger = logging.getLogger(__name__)\n\n' + source + '\n')
    text = converter.read_text()
    if 'from .filters import build_audio_filters' not in text:
        text = text.replace('from .process_runner import', 'from .filters import build_audio_filters\nfrom .process_runner import')
        converter.write_text(text)
    method(converter, "AudioConverter", "_build_audio_filters", '''
    def _build_audio_filters(self, settings):
        return build_audio_filters(settings, self.gtcrn_ladspa_path)
    ''')
    path = ROOT / "app/audio/player.py"
    text = path.read_text()
    if 'from .filters import build_audio_filters' not in text:
        text = text.replace('from .models import', 'from .filters import build_audio_filters\nfrom .models import')
        path.write_text(text)
    method(path, "AudioPlayer", "_preview_settings", '''
    def _preview_settings(self):
        frequencies = (31, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)
        equalizer = dict(self.equalizer_settings)
        return dict(volume=self.volume, speed=1.0,
                    eq_enabled=bool(equalizer), eq_bands=",".join(str(equalizer.get(f, 0)) for f in frequencies),
                    noise_reduction=self.noise_reduction, noise_strength=self.noise_strength,
                    noise_model=self.noise_model, noise_speech_strength=self.noise_speech_strength,
                    noise_lookahead=self.noise_lookahead, noise_voice_enhance=self.noise_voice_enhance,
                    noise_model_blend=self.noise_model_blend,
                    hpf_enabled=self.hpf_enabled, hpf_frequency=self.hpf_frequency,
                    transient_enabled=self.transient_enabled, transient_attack=self.transient_attack,
                    gate_enabled=self.gate_enabled, gate_intensity=self.gate_intensity,
                    compressor_enabled=self.compressor_enabled, compressor_intensity=self.compressor_intensity)
    ''')
    method(path, "AudioPlayer", "_rebuild_audio_filters", '''
    def _rebuild_audio_filters(self):
        if self._disposed or self.mpv_instance is None:
            return
        try:
            chain = []
            if not self.bypass_processing:
                effects = build_audio_filters(self._preview_settings(), self.gtcrn_ladspa_path)
                if effects:
                    chain.append("lavfi=[" + ",".join(effects) + "]")
                if self.pitch_correction:
                    # mpv's default scaletempo2 mutes speeds below 0.25x.
                    chain.append("scaletempo2=min-speed=0.1:max-speed=5")
                if self.normalize_enabled:
                    chain.append("lavfi=[loudnorm=I=-16:LRA=11:TP=-1.5]")
            value = ",".join(chain)
            if value != self._last_filter_chain:
                self.mpv_instance["af"] = value
                self._last_filter_chain = value
            # Export gain is in the DSP chain, before speed and normalization.
            self.mpv_instance.volume = 100
            self.mpv_instance.speed = 1 if self.bypass_processing else self.speed
        except Exception as error:
            logger.warning("Audio preview processing failed: %s", error)
            self._emit("error", str(error))
    ''')
    method(path, "AudioPlayer", "set_volume", '''
    def set_volume(self, volume):
        """Use the export's linear gain at the same position in the DSP chain."""
        self.volume = finite_number(volume, "Volume", 0, 10)
        self._rebuild_audio_filters()
    ''')
    method(path, "AudioPlayer", "set_normalize_enabled", '''
    def set_normalize_enabled(self, enabled):
        self.normalize_enabled = bool(enabled)
        self._rebuild_audio_filters()
    ''')
    method(path, "AudioPlayer", "set_bypass_processing", '''
    def set_bypass_processing(self, enabled):
        """Preview copy mode without destroying saved effect settings."""
        self.bypass_processing = bool(enabled)
        self._rebuild_audio_filters()
    ''')
    method(path, "AudioPlayer", "set_noise_reduction", '''
    def set_noise_reduction(self, enabled):
        if enabled and not self.gtcrn_ladspa_path:
            self.noise_reduction = False
            self._emit("error", _("Neural noise reduction is unavailable. Install the GTCRN plugin and models."))
            return False
        self.noise_reduction = bool(enabled)
        self._rebuild_audio_filters()
        return True
    ''')
