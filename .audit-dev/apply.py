"""Apply asynchronous media inspection and stable queue identity handling."""

from pathlib import Path
import subprocess
import queue_async
import queue_rows
import queue_mutations

subprocess.run(["git", "merge-base", "--is-ancestor", "690a3c6b9ed219ac9baec07b7401e61cb8c4b277", "HEAD"], check=True)
Path("scripts").mkdir(exist_ok=True)
queue_async.apply()
queue_rows.apply()
queue_mutations.apply()
editor_tests = Path("tests/gui/test_editor.py")
text = editor_tests.read_text().replace("import os\n", "import os\nimport uuid\n")
text = text.replace('Adw.Application(flags=Gio.ApplicationFlags.NON_UNIQUE)', 'Adw.Application(application_id="org.biglinux.EditorTest" + uuid.uuid4().hex, flags=Gio.ApplicationFlags.NON_UNIQUE)')
editor_tests.write_text(text)
workflow = Path('.github/workflows/audit-development.yml')
text = workflow.read_text().replace('desktop-file-utils xvfb', 'at-spi2-core python3-pyatspi desktop-file-utils xvfb')
workflow.write_text(text)
print("Applied asynchronous queue changes; native and graphical verification follow.")
