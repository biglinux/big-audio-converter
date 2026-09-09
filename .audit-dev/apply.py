"""Apply waveform, preferences and native-player remediation on the audit branch."""

from pathlib import Path
import subprocess
import waveform_ui
import configuration
import player_lifecycle
import player_transport
import player_effects

subprocess.run(["git", "merge-base", "--is-ancestor", "35c3e11b066ec080e15bf1993aac7f25fd4a5c89", "HEAD"], check=True)
Path("scripts").mkdir(exist_ok=True)
waveform_ui.apply()
configuration.apply()
player_lifecycle.apply()
player_transport.apply()
player_effects.apply()
print("Applied source changes; native regression tests follow.")
