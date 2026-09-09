"""Expose accessible editing and remove preview/export control discrepancies."""

import ast
from editing import ROOT, method, source_method


def apply():
    window = ROOT / "app/ui/main_window.py"
    text = window.read_text()
    if 'from app.ui.segment_editor import SegmentEditor' not in text:
        text = text.replace('from app.ui.conversion_session import', 'from app.ui.segment_editor import SegmentEditor\nfrom app.ui.conversion_session import')
    window.write_text(text)
    setup = source_method(window, "MainWindow", "setup_ui")
    anchor = '    self.right_header = HeaderBar('
    # Attach the editor to the queue area, not to hover-only waveform controls.
    if 'self.edit_segments_button =' not in setup:
        setup += '''\n    self.edit_segments_button = Gtk.Button(label=_("Edit Segments…"), halign=Gtk.Align.START)
    self.edit_segments_button.connect("clicked", self.on_edit_segments)
    self.edit_segments_button.set_tooltip_text(_("Edit start and end times using the keyboard"))
    self.right_content.append(self.edit_segments_button)
'''
    method(window, "MainWindow", "setup_ui", setup)
    method(window, "MainWindow", "on_edit_segments", '''
    def on_edit_segments(self, *args):
        identifier = self.active_audio_id
        duration = self.visualizer.duration
        if not identifier or duration <= 0:
            self._show_error_dialog(_("Select a File"), _("Select a file and wait for its duration to load before editing segments."))
            return
        def apply(segments):
            if self._disposed or identifier != self.active_audio_id:
                return False
            self.file_markers[identifier] = segments
            if self.cut_row.get_selected() == 0:
                self.cut_row.set_selected(1)
            if segments:
                self.visualizer.restore_markers(segments)
            else:
                self.visualizer.clear_all_markers()
            self.visualizer.queue_draw()
            return True
        dialog = SegmentEditor(duration, self.visualizer.get_marker_pairs(), apply, self.player.seek)
        self.segment_editor = dialog
        dialog.present(self)
    ''')
    settings = ROOT / "app/ui/settings_mixin.py"
    method(settings, "SettingsManagerMixin", "_on_volume_spin_changed", '''
    def _on_volume_spin_changed(self, spin):
        volume = spin.get_value()
        self.app.config.set("conversion_volume", str(volume))
        self.player.set_volume(volume / 100.0)
    ''')
    method(settings, "SettingsManagerMixin", "_on_normalize_switch_changed", '''
    def _on_normalize_switch_changed(self, row, pspec):
        self.app.config.set("normalize_enabled", str(row.get_active()).lower())
        self.player.set_normalize_enabled(row.get_active())
    ''')
    source = source_method(settings, "SettingsManagerMixin", "_set_copy_mode_ui")
    first = source.index('        # Reset volume')
    last = source.index('        # Close volume/speed', first)
    source = source[:first] + source[last:]
    source += '''\n    self.player.set_bypass_processing(is_copy_mode)
    if hasattr(self, "voice_expander"):
        self.voice_expander.set_sensitive(not is_copy_mode)
    self.format_row.set_subtitle(_("No re-encoding. Cuts follow packet boundaries and may be approximate.") if is_copy_mode else "")
'''
    method(settings, "SettingsManagerMixin", "_set_copy_mode_ui", source)
    source = source_method(settings, "SettingsManagerMixin", "setup_conversion_options")
    source = source.replace('noise_group = Adw.PreferencesGroup(title=_("Noise Reduction"))', 'noise_group = Adw.PreferencesGroup(title=_("Audio Effects"))\n    self.voice_expander = Adw.ExpanderRow(title=_("Voice Processing"))\n    self.voice_expander.set_subtitle(_("Optional tools for speech; each effect has its own switch"))\n    noise_group.add(self.voice_expander)')
    source = source.replace('Adw.ExpanderRow(title=_("Enable"))', 'Adw.ExpanderRow(title=_("Neural Noise Reduction"))')
    source = source.replace('noise_group.add(self.noise_expander)', 'self.voice_expander.add_row(self.noise_expander)')
    for name in ('gate_expander', 'compressor_expander', 'hpf_row', 'hpf_freq_row', 'transient_row', 'transient_attack_row'):
        source = source.replace('self.noise_expander.add_row(self.' + name + ')', 'self.voice_expander.add_row(self.' + name + ')')
    source = source.replace('_("EBU R128 standard (-16 LUFS)")', '_("Target: -16 LUFS; true-peak limit: -1.5 dBTP")')
    source += '''\n    if not self.converter.gtcrn_ladspa_path:
        self.noise_switch.set_sensitive(False)
        self.noise_expander.set_subtitle(_("Unavailable: install the GTCRN plugin and models"))
        self.transient_row.set_sensitive(False)
        self.transient_row.set_subtitle(_("Unavailable: install the transient-processing plugin"))
    self.voice_expander.set_expanded(False)
    self.player.set_bypass_processing(self._format_list[self.format_row.get_selected()] == "copy")
'''
    method(settings, "SettingsManagerMixin", "setup_conversion_options", source)
    method(settings, "SettingsManagerMixin", "_on_noise_switch_changed", '''
    def _on_noise_switch_changed(self, switch, state):
        """This switch controls neural denoising, not independently selected effects."""
        if state and not self.converter.gtcrn_ladspa_path:
            self.app.config.set("conversion_noise_reduction", "false")
            self._sources.idle(switch.set_active, False)
            return True
        self.app.config.set("conversion_noise_reduction", str(state).lower())
        self.noise_expander.set_enable_expansion(state)
        self.player.set_noise_reduction(state)
        if not state:
            self.noise_expander.set_expanded(False)
        return False
    ''')
    # Keep exact millisecond arithmetic centralized instead of truncating floats.
    markers = ROOT / "app/ui/marker_manager.py"
    text = markers.read_text().replace('from app.utils.time_formatter import', 'from app.audio.models import format_timestamp\nfrom app.utils.time_formatter import')
    markers.write_text(text)
    method(markers, "MarkerManagerMixin", "_format_time", '''
    def _format_time(self, time_in_seconds):
        return format_timestamp(time_in_seconds)
    ''')
    for path in (ROOT / "app/ui").glob("*.py"):
        text = path.read_text()
        # GTK 4.12+ string API avoids a deprecated bytes/length override.
        text = text.replace('.load_from_data(css.encode())', '.load_from_string(css)')
        text = text.replace('.load_from_data(css.encode("utf-8"))', '.load_from_string(css)')
        path.write_text(text)
