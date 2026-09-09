"""Apply asynchronous queue changes without asking CI to modify workflows."""

from pathlib import Path
import subprocess
import queue_async
import queue_rows
import queue_mutations
from editing import ROOT, method

subprocess.run(["git", "merge-base", "--is-ancestor", "690a3c6b9ed219ac9baec07b7401e61cb8c4b277", "HEAD"], check=True)
Path("scripts").mkdir(exist_ok=True)
queue_async.apply()
queue_rows.apply()
queue_mutations.apply()
method(ROOT / 'app/ui/file_queue.py', 'FileQueueRow', 'set_metadata', '''
def set_metadata(self, metadata_text):
    """Media tags are untrusted text, never application markup."""
    self.set_subtitle(GLib.markup_escape_text(str(metadata_text)))
''')
editor_tests = Path("tests/gui/test_editor.py")
text = editor_tests.read_text().replace("import os\n", "import os\nimport uuid\n")
text = text.replace('Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)', 'Adw.Application(application_id="org.biglinux.EditorTest" + uuid.uuid4().hex, flags=Gio.ApplicationFlags.NON_UNIQUE)')
editor_tests.write_text(text)
print("Applied asynchronous queue changes; native and graphical verification follow.")
