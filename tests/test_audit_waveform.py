"""Waveform accuracy, bounded memory and main-context lifecycle contracts."""

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
from hypothesis import given, settings, strategies as st
from gi.repository import GLib

from test_audit_audio import run, tone
from app.audio.envelope import PeakEnvelope
from app.audio.models import MediaSource
from app.audio.waveform import decode_envelope
from app.audio.waveform_jobs import WaveformJobs
from app.utils.main_context import SourceGroup


@settings(max_examples=40, deadline=None)
@given(st.lists(st.floats(min_value=-1, max_value=1, allow_nan=False, allow_infinity=False, width=32), min_size=1, max_size=2048), st.integers(1, 129))
def test_peak_envelope_matches_independent_blocks(values, chunk):
    array = np.asarray(values, dtype="<f4")
    stereo = np.column_stack((array, -array)).astype("<f4")
    envelope = PeakEnvelope(48000, 2, capacity=64)
    data = stereo.tobytes()
    for start in range(0, len(data), chunk):
        envelope.feed(data[start:start + chunk])
    peaks, rate, duration = envelope.finish()
    expected = [np.max(np.abs(array[start:start + envelope.block_size])) for start in range(0, len(array), envelope.block_size)]
    assert np.array_equal(peaks, np.array(expected, dtype=np.float32))
    assert len(peaks) <= 64
    assert peaks.nbytes <= 64 * 4
    assert duration == len(array) / 48000
    assert rate == 48000 / envelope.block_size


@pytest.mark.parametrize("expression", ["0|0.5*sin(2*PI*997*t)", "0.5*sin(2*PI*997*t)|-0.5*sin(2*PI*997*t)", "0.5*sin(2*PI*8000*t)|0"])
def test_real_audio_channels_and_high_frequencies_remain_visible(tmp_path, expression):
    path = tmp_path / "channels.wav"
    run("ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"aevalsrc={expression}:s=48000:d=2", str(path))
    data, rate, duration = decode_envelope(MediaSource(str(path)), capacity=2048)
    assert np.median(data) > 0.4
    assert duration == pytest.approx(2)
    assert len(data) <= 2048


def pump(predicate, timeout=5):
    context = GLib.MainContext.default()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(0.005)
    assert predicate(), "Main-context operation did not finish within its deadline"


def test_idle_is_one_shot_even_when_callback_returns_a_tuple():
    group = SourceGroup()
    calls = []
    group.idle(lambda: (calls.append(1), None))
    pump(lambda: bool(calls))
    context = GLib.MainContext.default()
    for _ in range(100):
        context.iteration(False)
    group.close()
    assert calls == [1]


def test_disposed_owner_is_not_called():
    group = SourceGroup()
    calls = []
    group.idle(lambda: calls.append(1))
    group.close()
    context = GLib.MainContext.default()
    for _ in range(10):
        context.iteration(False)
    assert not calls


class VisualizerProbe:
    markers_enabled = False

    def __init__(self):
        self.updates = []
        self.is_loading = False
        self.analysis_error = None

    def set_waveform(self, data, duration):
        assert threading.current_thread() is threading.main_thread()
        self.updates.append((data, duration))
        self.is_loading = False

    def set_loading(self, loading):
        assert threading.current_thread() is threading.main_thread()
        self.is_loading = loading


def test_only_latest_waveform_request_is_delivered(tone):
    widget = VisualizerProbe()
    jobs = WaveformJobs(widget)
    try:
        for _ in range(100):
            jobs.request(str(tone), "ffmpeg", {}, None, {}, True)
        pump(lambda: any(data is not None for data, _ in widget.updates), timeout=10)
        assert sum(data is not None for data, _ in widget.updates) == 1
        assert widget.updates[-1][1] == pytest.approx(3)
    finally:
        jobs.close()
    assert not jobs._worker.is_alive()
    assert jobs._runner is None


def test_waveform_error_is_delivered_once(tmp_path):
    widget = VisualizerProbe()
    jobs = WaveformJobs(widget)
    try:
        jobs.request(str(tmp_path / "absent.wav"), "ffmpeg", {}, None, {}, True)
        pump(lambda: widget.analysis_error is not None)
        count = len(widget.updates)
        for _ in range(100):
            GLib.MainContext.default().iteration(False)
        assert len(widget.updates) == count
    finally:
        jobs.close()
