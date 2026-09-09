"""Apply the second remediation group to the authorized feature branch."""

import subprocess
import waveform_ui
import configuration

subprocess.run(["git", "merge-base", "--is-ancestor", "35c3e11b066ec080e15bf1993aac7f25fd4a5c89", "HEAD"], check=True)
waveform_ui.apply()
configuration.apply()
print("Applied waveform and preferences changes; native regression tests follow.")
