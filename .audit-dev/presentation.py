"""Adaptive native presentation without hover timers or retained tooltip widgets."""

import ast
from editing import ROOT, method, source_method

HELPER = '''
class TooltipHelper:
    """Use GTK's tooltip placement, theme and lifecycle instead of custom popovers."""
    def __init__(self, config_manager=None):
        import weakref
        self.config_manager = config_manager
        self._widgets = weakref.WeakKeyDictionary()

    def is_enabled(self):
        return self.config_manager is None or str(self.config_manager.get("show_mouseover_tips", "true")).lower() == "true"

    def add_tooltip(self, widget, tooltip_key, y_offset=0):
        """Retain the legacy signature; native GTK controls tooltip placement."""
        text = TOOLTIPS.get(tooltip_key)
        if text:
            self._widgets[widget] = text
            widget.set_tooltip_text(text if self.is_enabled() else None)

    def refresh(self):
        for widget, text in list(self._widgets.items()):
            widget.set_tooltip_text(text if self.is_enabled() else None)

    def hide(self, immediate=False):
        # Native tooltips are dismissed by GTK when focus/pointer context changes.
        self.refresh()

    def hide_all(self):
        self.refresh()

    def cleanup(self):
        for widget in list(self._widgets):
            widget.set_tooltip_text(None)
        self._widgets.clear()
'''


def apply():
    window = ROOT / 'app/ui/main_window.py'
    text = window.read_text().replace('self.right_content.append(self.edit_segments_button)', 'right_content.append(self.edit_segments_button)')
    text = text.replace('self.set_size_request(920, 600)', 'self.set_size_request(640, 480)')
    window.write_text(text)
    setup = source_method(window, 'MainWindow', 'setup_ui')
    setup = setup.replace('left_box.set_size_request(300, -1)', 'left_box.set_size_request(260, -1)')
    setup = setup.replace('right_box.set_size_request(620, -1)', 'right_box.set_size_request(360, -1)')
    first = setup.index('    css_provider.load_from_data(')
    last = setup.index('    Gtk.StyleContext.add_provider_for_display(', first)
    setup = setup[:first] + '''    css_provider.load_from_string("""
    .sidebar { background-color: @sidebar_bg_color; }
    .dark-controls-bar { background-color: @headerbar_bg_color; padding: 6px 12px; }
    .dark-controls-bar label { color: @headerbar_fg_color; }
    .dark-controls-bar button { color: @headerbar_fg_color; }
    """)
''' + setup[last:]
    setup += '''\n    breakpoint = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 850px"))
    breakpoint.add_setter(self.split_view, "orientation", Gtk.Orientation.VERTICAL)
    breakpoint.add_setter(self.split_view, "position", 220)
    self.add_breakpoint(breakpoint)
'''
    method(window, 'MainWindow', 'setup_ui', setup)
    source = source_method(window, 'MainWindow', '_on_sidebar_width_changed')
    lines = source.splitlines(keepends=True)
    lines[1:1] = ['    if self.split_view.get_orientation() == Gtk.Orientation.VERTICAL:\n', '        return\n']
    method(window, 'MainWindow', '_on_sidebar_width_changed', ''.join(lines))
    helper = ROOT / 'app/utils/tooltip_helper.py'
    old = helper.read_text()
    tree = ast.parse(old)
    dictionary = next(node.value for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'TOOLTIPS' for target in node.targets))
    overrides = {
        'format': 'Choose the output format. Copy mode keeps encoded audio and uses approximate cuts.',
        'bitrate': 'Choose the target bitrate. Suitable values depend on the codec, channels and content.',
        'volume': '100% keeps the original gain. Higher gain can cause clipping.',
        'noise_reduction': 'Optional neural denoising for speech. It can damage music.',
        'waveform_visualizer': 'Seek in the upper area or mark cuts below. Edit Segments provides a keyboard-accessible alternative.',
        'normalize': 'Normalize to -16 LUFS with a -1.5 dBTP true-peak limit. This is not the EBU R128 broadcast target.',
    }
    entries = []
    for key, value in zip(dictionary.keys, dictionary.values):
        if not isinstance(key, ast.Constant) or not isinstance(value, ast.Call):
            raise RuntimeError('Unexpected tooltip dictionary syntax')
        label = overrides.get(key.value, value.args[0].value.split('\n\n')[0])
        entries.append(f'    {key.value!r}: _({label!r}),')
    helper.write_text('"""Native, concise contextual help for the audio converter."""\n\nimport gettext\n\n_ = gettext.gettext\n\nTOOLTIPS = {\n' + '\n'.join(entries) + '\n}\n\n' + HELPER)
    source = source_method(window, 'MainWindow', '_on_tips_action_changed')
    source += '\n    self.tooltip_helper.refresh()\n'
    method(window, 'MainWindow', '_on_tips_action_changed', source)
    controls = ROOT / 'app/ui/controls_bar_mixin.py'
    source = source_method(controls, 'ControlsBarMixin', '_on_volume_scale_changed')
    source = source.replace('    if player_volume > 1.0:\n        player_volume = 1.0 + (player_volume - 1.0) * 0.5\n', '')
    method(controls, 'ControlsBarMixin', '_on_volume_scale_changed', source)
    source = source_method(controls, 'ControlsBarMixin', '_on_zoom_scale_changed')
    source = source[:source.index('    # Schedule auto-close')]
    method(controls, 'ControlsBarMixin', '_on_zoom_scale_changed', source)
    # Popovers open on an intentional click/keyboard activation. Native autohide
    # handles outside clicks and Escape, without racing slow keyboard interaction.
    tree = ast.parse(controls.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    for function in cls.body:
        if isinstance(function, ast.FunctionDef) and ('_hover_enter' in function.name or '_hover_leave' in function.name):
            method(controls, cls.name, function.name, f'def {function.name}(self, *args):\n    return None')
