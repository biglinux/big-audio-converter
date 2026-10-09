"""Adversarial I/O and property-based contracts for file and process ownership."""

import concurrent.futures
import errno
import json
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest
from app.audio.media import MediaSource
from app.audio.output import publish_all
from app.audio.process import CancelledError, MediaError, ProcessRunner
from app.audio.waveform import PeakCollector
from app.utils.config import AppConfig
from app.utils.main_loop import MainLoopSources


@pytest.mark.parametrize(
    "raw",
    [
        b"\x00",
        np.asarray([float("nan")], dtype="<f4").tobytes(),
        np.asarray([float("inf")], dtype="<f4").tobytes(),
        b"",
    ],
)
def test_invalid_pcm_does_not_publish_a_waveform(raw):
    collector = PeakCollector(64, 48000)
    with pytest.raises(MediaError):
        collector.consume(raw)
        collector.finish()


def test_stdout_and_stderr_are_drained_independently():
    runner = ProcessRunner()
    result = runner.run(
        [
            sys.executable,
            "-c",
            "import os; os.write(2,b'x'*262144); os.write(1,b'done')",
        ],
        timeout=3,
    )
    assert result.stdout == "done" and len(result.stderr) == runner.DIAGNOSTIC_LIMIT
    assert runner.process is None


def test_excessive_probe_output_fails_without_retaining_a_process():
    runner = ProcessRunner()
    with pytest.raises(MediaError):
        runner.run(
            [sys.executable, "-c", "import os; os.write(1,b'x'*100000)"],
            output_limit=100,
            timeout=3,
        )
    assert runner.process is None


def test_cancel_reaps_a_process_ignoring_term_and_closing_its_pipes():
    runner = ProcessRunner()
    failures = []

    def worker():
        try:
            runner.run(
                [
                    sys.executable,
                    "-c",
                    "import os,signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); os.write(1,b'ready'); os.close(1); os.close(2); time.sleep(30)",
                ]
            )
        except CancelledError:
            failures.append("cancelled")

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 3
    while runner.process is None and time.monotonic() < deadline:
        time.sleep(0.005)
    process = runner.process
    assert process is not None
    time.sleep(0.1)
    started = time.monotonic()
    runner.cancel()
    thread.join(2)
    assert not thread.is_alive() and time.monotonic() - started < 2
    assert failures == ["cancelled"]
    assert process.returncode in (-signal.SIGKILL, -signal.SIGTERM)
    assert not Path(f"/proc/{process.pid}").exists()
    assert runner.process is None


def test_timeout_reaps_process():
    runner = ProcessRunner()
    with pytest.raises(MediaError, match="timed out"):
        runner.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.1)
    assert runner.process is None


def test_publication_is_safe_during_real_concurrent_collisions(tmp_path):
    requested = tmp_path / "same.wav"
    requested.write_bytes(b"existing")
    files = []
    for index in range(20):
        path = tmp_path / f"staged-{index}.wav"
        path.write_bytes(str(index).encode())
        files.append(path)

    def publish(path):
        return publish_all([(path, requested)], ProcessRunner())[0]

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(publish, files))
    assert len(set(results)) == 20
    assert {Path(path).read_bytes() for path in results} == {
        str(i).encode() for i in range(20)
    }
    assert requested.read_bytes() == b"existing"


def test_publication_rolls_back_only_its_own_files_on_failure(tmp_path):
    source = tmp_path / "staged.wav"
    source.write_bytes(b"new")
    target = tmp_path / "published.wav"
    with pytest.raises(FileNotFoundError):
        publish_all(
            [(source, target), (tmp_path / "missing.wav", tmp_path / "other.wav")],
            ProcessRunner(),
        )
    assert not target.exists()


def test_publication_without_hard_links_never_replaces_a_file(tmp_path, monkeypatch):
    from app.audio import output as module

    def no_links(*_args, **_kwargs):
        raise PermissionError(errno.EPERM, "hard links unsupported")

    monkeypatch.setattr(module, "_renameat2", None)
    monkeypatch.setattr(module.os, "link", no_links)
    requested = tmp_path / "same.wav"
    requested.write_bytes(b"existing")
    staged = tmp_path / "staged.wav"
    staged.write_bytes(b"new")
    published = publish_all([(staged, requested)], ProcessRunner())[0]
    assert requested.read_bytes() == b"existing"
    assert Path(published).read_bytes() == b"new"
    assert not staged.exists()


def test_dangling_symlink_is_not_overwritten(tmp_path):
    missing = tmp_path / "missing"
    target = tmp_path / "file.wav"
    target.symlink_to(missing)
    source = tmp_path / "staged"
    source.write_bytes(b"new")
    output = publish_all([(source, target)], ProcessRunner())[0]
    assert target.is_symlink() and not missing.exists()
    assert Path(output).read_bytes() == b"new"


@pytest.mark.parametrize("text", ["[]", "{broken", '{"conversion_volume":NaN}', "null"])
def test_corrupt_config_survives_until_an_atomic_replacement(tmp_path, text):
    file = tmp_path / "config.json"
    file.write_text(text)
    config = AppConfig(tmp_path)
    assert config.load_warning
    assert file.read_text() == text
    config.set("conversion_volume", 75)
    config.close()
    assert json.loads(file.read_text())["conversion_volume"] == 75
    assert next(tmp_path.glob("config.invalid-*.json")).read_text() == text
    assert file.stat().st_mode & 0o777 == 0o600


def test_two_config_instances_preserve_independent_updates(tmp_path):
    a, b = AppConfig(tmp_path), AppConfig(tmp_path)
    a.set("conversion_volume", 85)
    b.set("conversion_speed", 1.25)
    a.close()
    b.close()
    result = json.loads((tmp_path / "config.json").read_text())
    assert result["conversion_volume"] == 85 and result["conversion_speed"] == 1.25


def test_one_shot_callbacks_do_not_repeat_even_when_returning_truthy():
    from gi.repository import GLib

    owner, calls = MainLoopSources(), []
    owner.idle(lambda: (calls.append(1), True))
    context = GLib.MainContext.default()
    for _ in range(20):
        if context.pending():
            context.iteration(False)
    assert calls == [1]
    owner.idle(lambda: calls.append(2))
    owner.close()
    for _ in range(20):
        if context.pending():
            context.iteration(False)
    assert calls == [1]


def test_real_filename_in_track_namespace_is_still_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("bac-track:legitimate.wav").write_bytes(b"file")
    assert MediaSource.resolve("bac-track:legitimate.wav").path == str(
        tmp_path / "bac-track:legitimate.wav"
    )
