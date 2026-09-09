"""Support fixed internal destinations as well as collision-renamed exports."""

from editing import ROOT, method, source_method


def apply():
    path = ROOT / "app/audio/output_transaction.py"
    method(path, "OutputTransaction", "__init__", '''
    def __init__(self, destinations, rename_on_conflict=True):
        self.destinations = [str(Path(p).absolute()) for p in destinations]
        self.rename_on_conflict = rename_on_conflict
        self.staged = []
        self._directories = []
        self._published = []
        self._committed = False
    ''')
    source = source_method(path, "OutputTransaction", "__enter__")
    source = source.replace('            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)\n            os.close(fd)\n', '')
    method(path, "OutputTransaction", "__enter__", source)
    source = source_method(path, "OutputTransaction", "commit")
    if 'if self.rename_on_conflict else requested' not in source:
        source = source.replace('        target = available_path(requested)', '        target = available_path(requested) if self.rename_on_conflict else requested', 1)
        source = source.replace('            except FileExistsError:\n                target = available_path(requested)', '            except FileExistsError:\n                if not self.rename_on_conflict:\n                    raise\n                target = available_path(requested)')
    method(path, "OutputTransaction", "commit", source)
