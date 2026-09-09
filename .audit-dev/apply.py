"""Apply audit changes only in the explicitly authorized feature branch."""

from pathlib import Path
import subprocess
import backend
import segments
import outputs
import regression_tests
import finalize_core

head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
base = "95f02c3a019af7cb40813fa38af25c7b3906c99c"
subprocess.run(["git", "merge-base", "--is-ancestor", base, head], check=True)
for directory in ("docs", "scripts"):
    Path(directory).mkdir(exist_ok=True)
outputs.apply()
backend.apply()
segments.apply()
regression_tests.apply()
finalize_core.apply()
print("Applied source transformations. Runtime tests follow in a separate step.")
