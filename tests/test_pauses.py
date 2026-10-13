"""Pauses found on DPDFNet-cleaned speech become cuts that keep a little air."""

import re
import shutil
import subprocess

import pytest
from app.audio.media import MediaSource
from app.audio.pauses import KEEP, STEP, find_pauses, find_speech, measure_levels
from app.audio.process import ProcessRunner


def levels(*runs):
    """An envelope from (level dB, seconds) runs."""
    return [level for level, seconds in runs for _ in range(round(seconds / STEP))]


def test_pauses_keep_air_only_beside_speech():
    envelope = levels((-80, 1), (-6, 2), (-70, 1.5), (-6, 2), (-80, 1))
    assert find_pauses(envelope, 0.5) == [
        {"start": 0.0, "stop": 1 - KEEP},
        {"start": 3 + KEEP, "stop": 4.5 - KEEP},
        {"start": 6.5 + KEEP, "stop": 7.5},
    ]


def test_speech_lies_between_the_pauses():
    envelope = levels((-80, 1), (-6, 2), (-70, 1.5), (-6, 2), (-80, 1))
    assert find_speech(envelope, 0.5) == [
        {"start": 1 - KEEP, "stop": 3 + KEEP},
        {"start": 4.5 - KEEP, "stop": 6.5 + KEEP},
    ]
    # Without pauses the whole file is speech; without levels there is none.
    assert find_speech(levels((-6, 3)), 0.5) == [{"start": 0.0, "stop": 3.0}]
    assert find_speech([], 0.5) == []


def test_only_long_silences_are_pauses():
    assert find_pauses(levels((-6, 1), (-70, 0.4), (-6, 1)), 0.5) == []
    assert find_pauses(levels((-6, 3)), 0.5) == []
    assert find_pauses([], 0.5) == []


def test_threshold_follows_speech_level_within_bounds():
    # Quiet speech at -40 dBFS keeps a -65 dBFS floor: DPDFNet's residue
    # (about -70) is a pause, a -60 dBFS murmur is not.
    quiet = levels((-40, 2), (-70, 1), (-40, 2))
    assert find_pauses(levels((-40, 2), (-60, 1), (-40, 2)), 0.5) == []
    assert find_pauses(quiet, 0.5) == [{"start": 2 + KEEP, "stop": 3 - KEEP}]
    # Loud speech cannot lift the threshold above -40 dBFS and eat soft words.
    loud = levels((0, 2), (-38, 1), (0, 2))
    assert find_pauses(loud, 0.5) == []
    digital_silence = levels((float("-inf"), 2))
    assert find_pauses(digital_silence, 0.5) == [{"start": 0.0, "stop": 2.0}]


def ffmpeg(*args):
    return subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", *args],
        capture_output=True,
        text=True,
        check=True,
    )


def denoiser_with_voice_sensitivity():
    denoiser = shutil.which("dpdfnet-enhance")
    return (
        denoiser
        and "--voice-sensitivity"
        in subprocess.run(
            [denoiser, "--help"], capture_output=True, text=True, check=False
        ).stdout
    )


@pytest.mark.skipif(
    not (denoiser_with_voice_sensitivity() and shutil.which("espeak-ng")),
    reason="needs dpdfnet-enhance with --voice-sensitivity, and espeak-ng",
)
@pytest.mark.parametrize("peak", [-30, -20, -10])
def test_noisy_pause_is_found_at_edges(tmp_path, peak):
    for name, text in (
        ("one", "The quick brown fox jumps over the lazy dog."),
        ("two", "Then it runs back into the forest."),
    ):
        subprocess.run(
            ["espeak-ng", "-w", str(tmp_path / f"{name}.wav"), text], check=True
        )
    clean = tmp_path / "clean.wav"
    ffmpeg(
        "-v",
        "error",
        "-i",
        str(tmp_path / "one.wav"),
        "-i",
        str(tmp_path / "two.wav"),
        "-filter_complex",
        "[0]apad=pad_dur=1.5[a];[a][1]concat=v=0:a=1,aresample=48000",
        str(clean),
    )
    report = ffmpeg(
        "-i",
        str(clean),
        "-af",
        "volumedetect,silencedetect=noise=-50dB:d=0.5",
        "-f",
        "null",
        "-",
    ).stderr
    gain = peak - float(re.search(r"max_volume: (\S+) dB", report)[1])
    start = float(re.search(r"silence_start: (\S+)", report)[1])
    stop = float(re.search(r"silence_end: (\S+)", report)[1])
    # White noise at -30 dBFS, as loud as the quietest speech.
    noisy = tmp_path / "noisy.wav"
    ffmpeg(
        "-v",
        "error",
        "-i",
        str(clean),
        "-filter_complex",
        f"[0]volume={gain}dB[s];anoisesrc=r=48000:a=0.03:d=30:seed=7[n];"
        "[s][n]amix=normalize=0:duration=first",
        str(noisy),
    )

    denoiser = shutil.which("dpdfnet-enhance")
    levels = measure_levels(
        MediaSource(str(noisy)), "ffmpeg", denoiser, 3, ProcessRunner()
    )
    found = find_pauses(levels, 0.5)

    # Never into the speech; at speech as quiet as the noise, DPDFNet takes
    # part of a word's fading tail, leaving less air than KEEP.
    assert len(found) == 1
    assert start < found[0]["start"] <= start + KEEP + 0.05
    assert stop - KEEP - 0.05 <= found[0]["stop"] < stop


def test_an_unexpected_failure_ends_the_analysis(monkeypatch, tmp_path):
    from app.audio import pauses
    from conftest import pump

    def broken(*_args):
        raise RuntimeError("decoder crashed")

    monkeypatch.setattr(pauses, "measure_levels", broken)
    finder, failures = pauses.PauseFinder(), []
    source = MediaSource(str(tmp_path / "missing.wav"))
    finder.start(
        None, source, "ffmpeg", "dpdfnet-enhance", 3, None, None, failures.append
    )
    pump(lambda: failures)
    assert not finder.busy
    assert failures == ["The pauses could not be measured."]


def fake_denoiser(tmp_path, script):
    denoiser = tmp_path / "dpdfnet-enhance"
    denoiser.write_text("#!/bin/sh\n" + script)
    denoiser.chmod(0o755)
    audio = tmp_path / "speech.wav"
    ffmpeg("-v", "error", "-f", "lavfi", "-i", "sine=d=1", str(audio))
    return MediaSource(str(audio)), str(denoiser)


@pytest.mark.parametrize(
    "refusal",
    [
        # Before --speech-only: its usage.
        "dpdfnet-enhance: usage: dpdfnet-enhance [--channels N]",
        # Before --voice-sensitivity: the option by name.
        'dpdfnet-enhance: unknown argument "--voice-sensitivity"',
    ],
)
def test_old_denoiser_asks_for_update(tmp_path, refusal):
    from app.audio.process import MediaError

    source, old = fake_denoiser(tmp_path, f"echo '{refusal}' >&2\nexit 2\n")
    with pytest.raises(MediaError, match="Update dpdfnet-native"):
        measure_levels(source, "ffmpeg", old, 3, ProcessRunner())


def test_voice_sensitivity_reaches_denoiser(tmp_path):
    args = tmp_path / "args"
    # Records its options and lets the audio through.
    source, denoiser = fake_denoiser(tmp_path, f'echo "$@" > "{args}"\nexec cat\n')
    levels = measure_levels(source, "ffmpeg", denoiser, 5, ProcessRunner())
    assert len(levels) == round(1 / STEP)
    assert "--speech-only --voice-sensitivity 5" in args.read_text()
