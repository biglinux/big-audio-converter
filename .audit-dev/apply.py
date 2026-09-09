"""Apply the current, idempotent corrective group on the feature branch."""

from pathlib import Path
import subprocess
import correct_regressions

subprocess.run(["git", "merge-base", "--is-ancestor", "2d7fc16ce12e15adafb6a8212d8f440d62f485ba", "HEAD"], check=True)
Path("scripts").mkdir(exist_ok=True)
correct_regressions.apply()
print("Corrective source changes applied; native tests follow.")
