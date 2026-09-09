"""Schedule waveform work on one owned worker, not a new thread per request."""

import ast
from editing import ROOT, method, source_method


def apply():
    for path in (ROOT / "app/ui").glob("*.py"):
        text = path.read_text()
        lines = text.splitlines(keepends=True)
        edits = []
        for node in ast.walk(ast.parse(text)):
            if not isinstance(node, ast.Expr) or not isinstance(node.value, ast.Call):
                continue
            call = node.value
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "start":
                continue
            constructor = call.func.value
            if not isinstance(constructor, ast.Call):
                continue
            keywords = {item.arg: item.value for item in constructor.keywords}
            target = keywords.get("target")
            args = keywords.get("args")
            if not isinstance(target, ast.Attribute) or not isinstance(target.value, ast.Name):
                continue
            if target.value.id != "waveform" or target.attr not in ("generate", "activate_without_waveform"):
                continue
            if args is None:
                raise RuntimeError("Waveform thread has no explicit argument tuple")
            arguments = ast.get_source_segment(text, args)
            enabled = target.attr == "generate"
            replacement = " " * node.col_offset + f"waveform.request(*{arguments}, enabled={enabled})\n"
            edits.append((node.lineno - 1, node.end_lineno, replacement))
        for first, last, replacement in sorted(edits, reverse=True):
            lines[first:last] = [replacement]
        if edits:
            path.write_text("".join(lines))
    path = ROOT / "app/ui/visualizer.py"
    text = path.read_text()
    if 'from app.audio import waveform' not in text:
        text = text.replace('import gettext\n', 'import gettext\nfrom app.audio import waveform\n')
    text = text.replace('_("No audio loaded")', 'getattr(self, "analysis_error", None) or _("No audio loaded")')
    path.write_text(text)
    clear = source_method(path, "AudioVisualizer", "clear_waveform")
    if 'waveform.cancel(self)' not in clear:
        clear = clear.replace('    self.is_loading = False', '    waveform.cancel(self)\n    self.is_loading = False', 1)
        method(path, "AudioVisualizer", "clear_waveform", clear)
    jobs = ROOT / "app/audio/waveform_jobs.py"
    text = jobs.read_text().replace('ffmpeg, markers or {},', 'ffmpeg, markers if markers is not None else {},')
    jobs.write_text(text)
