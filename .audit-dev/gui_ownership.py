"""Connect conversion/session ownership to normal window and application shutdown."""

from pathlib import Path
from editing import ROOT, method, source_method


def apply():
    path = ROOT / "app/ui/main_window.py"
    text = path.read_text()
    if 'from app.ui.conversion_session import ConversionSession' not in text:
        text = text.replace('from app.ui.controls_bar_mixin import', 'from app.ui.conversion_session import ConversionSession\nfrom app.utils.main_context import SourceGroup\nfrom app.ui.controls_bar_mixin import')
    path.write_text(text)
    init = source_method(path, "MainWindow", "__init__")
    if 'self._disposed = False' not in init:
        init = init.replace('    self.app = kwargs.get("application")', '    self.app = kwargs.get("application")\n    self.app._main_window = self\n    self._disposed = False\n    self._sources = SourceGroup()\n    self.conversion_session = None')
    method(path, "MainWindow", "__init__", init)
    method(path, "MainWindow", "_window_buttons_on_left", '''
    def _window_buttons_on_left(self):
        """An optional desktop preference must never abort application startup."""
        source = Gio.SettingsSchemaSource.get_default()
        schema = source.lookup("org.gnome.desktop.wm.preferences", True) if source else None
        if schema is None:
            return False
        settings = Gio.Settings.new_full(schema, None, None)
        return "close" in settings.get_string("button-layout").split(":", 1)[0]
    ''')
    convert = source_method(path, "MainWindow", "on_convert")
    if 'self.conversion_session.start' not in convert:
        first = convert.index('    # Create a progress dialog')
        convert = convert[:first] + '''    if self.conversion_session is None:
        self.conversion_session = ConversionSession(self)
    self.conversion_session.start(self.file_queue.get_files(), settings)
'''
        lines = convert.splitlines(keepends=True)
        lines[1:1] = ['    if self._disposed or (self.conversion_session and self.conversion_session.running):\n', '        return\n']
        convert = ''.join(lines)
        convert = convert.replace('            if current_markers:', '            if current_markers is not None:')
        method(path, "MainWindow", "on_convert", convert)
    method(path, "MainWindow", "on_close_request", '''
    def on_close_request(self, *args):
        """Keep the main loop alive until cancellation has been acknowledged."""
        if self.conversion_session is not None and self.conversion_session.request_close():
            return True
        self.shutdown()
        return False
    ''')
    method(path, "MainWindow", "shutdown", '''
    def shutdown(self):
        if self._disposed:
            return
        self._disposed = True
        if not self.is_maximized():
            self._save_window_size()
        if self.conversion_session is not None:
            self.conversion_session.close()
        jobs = getattr(self.visualizer, "_waveform_jobs", None)
        if jobs is not None:
            jobs.close()
        self.converter.cleanup()
        self.player.cleanup()
        close_queue = getattr(self.file_queue, "cleanup", None)
        if close_queue is not None:
            close_queue()
        if self.tooltip_helper:
            self.tooltip_helper.cleanup()
        self._sources.close()
        self.app.config.close()
    ''')
    # A single owner tracks sources created by the window and its mixins.
    for filename, class_name in (("main_window.py", "MainWindow"),
                                 ("controls_bar_mixin.py", "ControlsBarMixin"),
                                 ("settings_mixin.py", "SettingsManagerMixin"),
                                 ("playback_controller.py", "PlaybackControllerMixin")):
        file = ROOT / "app/ui" / filename
        import ast
        tree = ast.parse(file.read_text())
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
        for function in list(cls.body):
            if isinstance(function, ast.FunctionDef):
                source = source_method(file, class_name, function.name)
                updated = source.replace('GLib.idle_add(', 'self._sources.idle(').replace('GLib.timeout_add(', 'self._sources.timeout(').replace('GLib.source_remove(', 'self._sources.remove(')
                if source != updated:
                    method(file, class_name, function.name, updated)
    session = ROOT / "app/ui/conversion_session.py"
    text = session.read_text().replace('window.header_bar.convert_button', 'window.convert_button')
    session.write_text(text)
    converter = ROOT / "app/audio/converter.py"
    source = source_method(converter, "AudioConverter", "convert_all_files")
    source = source.replace('progress_callback, finish_callback):', 'progress_callback, finish_callback, *, reset=True):')
    source = source.replace('        self.reset_cancellation()', '        if reset:\n            self.reset_cancellation()')
    method(converter, "AudioConverter", "convert_all_files", source)
    entry = ROOT / "main.py"
    method(entry, "Application", "_present_window_and_request_focus", '''
    def _present_window_and_request_focus(self, window):
        """Respect the window manager's focus policy without transient-window tricks."""
        window.present()
    ''')
    method(entry, "Application", "on_quit_action", '''
    def on_quit_action(self, *args):
        window = getattr(self, "_main_window", None)
        if window is not None:
            window.close()
        else:
            self.quit()
    ''')
    method(entry, "Application", "do_shutdown", '''
    def do_shutdown(self):
        window = getattr(self, "_main_window", None)
        if window is not None:
            window.shutdown()
        from app.audio import waveform
        waveform.shutdown()
        self.player.cleanup()
        self.converter.cleanup()
        self.config.close()
        Adw.Application.do_shutdown(self)
    ''')
    # Debug output is needed even if a native assertion terminates the process.
    workflow = Path('.github/workflows/audit-development.yml')
    text = workflow.read_text().replace('-m pytest -q tests/gui', '-X faulthandler -m pytest -q -s tests/gui')
    workflow.write_text(text)
