"""Wire owned conversion sessions into the real desktop application."""

from pathlib import Path
import subprocess
import gui_ownership

subprocess.run(["git", "merge-base", "--is-ancestor", "a02fb57a4eb879cc0b8d50c2ed9d97d43bf29035", "HEAD"], check=True)
Path("scripts").mkdir(exist_ok=True)
gui_ownership.apply()
print("Applied UI ownership changes; native and graphical tests follow.")
