"""Apply accessible editing and native adaptive presentation."""

from pathlib import Path
import subprocess
import ui_access
import presentation

subprocess.run(["git", "merge-base", "--is-ancestor", "9def7affa4806eaca0ddeb29edeed67032006ef1", "HEAD"], check=True)
Path("scripts").mkdir(exist_ok=True)
ui_access.apply()
presentation.apply()
print("Applied accessible editing and presentation changes; runtime tests follow.")
