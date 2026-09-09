"""Real cancellation and pipe-draining tests with operating-system checks."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest
from test_audit_audio import tone, options
from app.audio.converter import AudioConverter
from app.audio.process_runner import ProcessRunner


@pytest.mark.parametrize("cut", [False, True])
def test_cancellation_reaps_ffmpeg_and_removes_staging(tone, tmp_path, cut):
    wrapper = tmp_path / "slow-ffmpeg"
    ffmpeg = shutil.which("ffmpeg")
    wrapper.write_text(f'#!/bin/sh\nexec "{ffmpeg}" -re "$@"\n')
    wrapper.chmod(0o700)
    converter = AudioConverter()
    converter.ffmpeg_path = str(wrapper)
    output = tmp_path / "cancelled.wav"
    settings = options(tone) if cut else {"format": "wav"}
    completed = []
    worker = threading.Thread(target=lambda: completed.append(converter.convert_file(str(tone), str(output), settings)))
    worker.start()
    child = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            runner = getattr(converter, "_runner", None)
            process = runner.process if runner is not None else None
            if process is not None and process.args[0] == str(wrapper):
                child = process.pid
                break
            time.sleep(0.01)
        assert child is not None, "The test must observe a real conversion before cancelling"
        converter.cancel_conversion()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert completed == [False]
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert not output.exists()
        assert not list(tmp_path.glob(".bac-*"))
    finally:
        converter.cancel_conversion()
        worker.join(timeout=3)


def test_diagnostics_are_drained_and_bounded():
    runner = ProcessRunner()
    result = runner.run([sys.executable, "-c", "import sys; sys.stderr.write('x'*300000); sys.stdout.write('done')"], timeout=5)
    assert result.returncode == 0
    assert result.stdout == b"done"
    assert len(result.stderr) == 65536
    assert runner.process is None


def test_timeout_reaps_the_process(monkeypatch):
    runner = ProcessRunner()
    seen = []
    original = runner._signal
    def record(process, sig):
        seen.append(process.pid)
        original(process, sig)
    monkeypatch.setattr(runner, "_signal", record)
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    assert seen
    for pid in set(seen):
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.parametrize("speed", [0, -1, float("nan"), float("inf")])
def test_invalid_speed_is_rejected_without_looping(speed):
    with pytest.raises(ValueError):
        AudioConverter()._build_audio_filters({"speed": speed})
