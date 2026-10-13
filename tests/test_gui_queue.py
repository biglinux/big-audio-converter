"""The file queue and its conversions, in the real window."""

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from conftest import pump

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")),
    reason="A graphical display is required",
)


def test_queue_probe_lifecycle_and_numeric_editor(window, audio):
    queue = window.file_queue
    assert queue.add_file(str(audio))
    assert queue.pending
    assert not window.convert_button.get_sensitive()
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert not window._test_errors
    assert len(queue.files) == 1
    assert window.seekbar.duration == pytest.approx(2)
    assert queue.file_rows[0].get_subtitle() == "0:02 · WAV · 44.1 kHz"
    assert window.convert_button.get_sensitive()
    window.on_edit_segments()
    editor = window.segment_editor
    editor.add_segment(0.1, 0.11)
    editor.apply()
    assert window.visualizer.get_marker_pairs()[0]["stop"] == pytest.approx(0.11)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    assert window.conversion.last_batch.items[0].successful
    output = Path(window.conversion.last_batch.outputs[0])
    assert output.is_file()
    assert queue.files == [str(audio)]  # Repeating an operation keeps the input.
    queue.remove_file(0)
    assert not queue.track_metadata
    assert not queue.pending


def test_join_needs_two_files_and_reencoding(window, audio, tmp_path):
    queue = window.file_queue
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    assert queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    assert not window.join_row.get_sensitive()
    assert queue.add_file(str(second))
    pump(lambda: not queue.pending)
    assert window.join_row.get_sensitive()
    window.join_row.set_active(True)
    assert window.convert_button.get_label() == "Join 2 files"
    assert window._collect_conversion_settings()["join_files"]
    window.format_row.set_selected(window._format_list.index("copy"))
    assert not window.join_row.get_sensitive()
    assert window.convert_button.get_label() == "Convert 2 files"
    assert not window._collect_conversion_settings()["join_files"]
    window.format_row.set_selected(window._format_list.index("flac"))
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    [output] = window.conversion.last_batch.outputs
    assert output.endswith("-joined.flac")


def test_pending_cuts_follow_each_track(window, tmp_path):
    video = tmp_path / "multi.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=48000:duration=2",
            "-map",
            "0",
            "-map",
            "1",
            "-c:a",
            "flac",
            str(video),
        ],
        check=True,
        timeout=10,
    )
    queue = window.file_queue
    assert queue.add_file(str(video))
    # main.py records a player's cuts while the file is still being inspected.
    window.file_markers[str(video)] = [{"start": 0.5, "stop": 1.0}]
    pump(lambda: not queue.pending)
    assert len(queue.files) == 2
    assert str(video) not in window.file_markers
    for identifier in queue.files:
        (cut,) = window.file_markers[identifier]
        assert (cut["start"], cut["stop"]) == pytest.approx((0.5, 1.0))


def test_removed_pending_row_stays_removed(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    queue.remove_file(0)
    pump(lambda: not queue._probes._active)
    assert queue.files == []
    assert queue.file_rows == []
    assert window.active_audio_id is None


def test_full_inspection_waits_without_timer(window, tmp_path, monkeypatch):
    from app.ui.file_queue import MAX_INSPECTING

    queue = window.file_queue
    tokens = iter(range(1, 10000))
    monkeypatch.setattr(queue._probes, "request", lambda *_a, **_k: next(tokens))
    queue.add_files([str(tmp_path / f"{n}.wav") for n in range(MAX_INSPECTING + 2)])
    pump(lambda: len(queue._pending_probes) == MAX_INSPECTING)
    pump()
    assert len(queue._imports) == 2
    assert queue._import_source is None
    path, token = next(iter(queue._pending_probes.items()))
    queue._probe_ready(token, path, None, "unreadable")
    pump(lambda: len(queue._imports) == 1)
    assert len(queue._pending_probes) == MAX_INSPECTING
    assert queue._import_source is None


def test_multitrack_expands_off_the_gtk_thread(window, tmp_path):
    video = tmp_path / "tracks.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:duration=2",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-c:a",
            "flac",
            str(video),
        ],
        check=True,
        timeout=10,
    )
    queue = window.file_queue
    assert queue.add_file(str(video))
    pump(lambda: not queue.pending)
    assert not window._test_errors
    assert len(queue.files) == 2
    assert {value["track_index"] for value in queue.track_metadata.values()} == {0, 1}
    assert not queue.add_file(str(video))
    queue.clear_queue()
    assert queue.track_metadata == {}
    assert queue.active_file_index is None
    assert queue.currently_playing_index is None


def test_information_dialog_opens_without_blocking(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    row = queue.file_rows[0]
    started = time.monotonic()
    row.info_button.emit("clicked")
    assert row._info_dialogs
    assert time.monotonic() - started < 0.5
    pump(lambda: not queue._probes._active)
    row.cleanup()
    assert not row._info_dialogs


def test_information_size_accounts_for_shared_tracks(audio):
    from app.ui.file_info import audio_properties, format_size, shares_file

    track = {"index": 0, "codec_type": "audio", "duration": "2.0"}
    cover = {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 1}}
    video = {"index": 1, "codec_type": "video", "disposition": {"attached_pic": 0}}
    assert not shares_file({"streams": [track]}, track)
    assert not shares_file({"streams": [track, cover]}, track)
    assert shares_file({"streams": [track, video]}, track)

    def size(stream, shares):
        info = {"streams": [stream], "format": {}}
        return dict(audio_properties(stream, info, shares, str(audio)))["Size"]

    assert size(track, False) == format_size(audio.stat().st_size)
    assert size(track, True) == "Unknown"
    assert size({**track, "bit_rate": "128000"}, True) == format_size(32000)


def test_command_line_paths_use_callers_directory(window, audio, tmp_path, monkeypatch):
    from gi.repository import Gio, GLib

    app = window.get_application()
    shutil.copyfile(audio, tmp_path / "relative.wav")
    line = Gio.ApplicationCommandLine(
        arguments=GLib.Variant(
            "aay",
            [b"big-audio-converter\0", b"relative.wav\0", b"bac-test://host/a.wav\0"],
        ),
        platform_data=GLib.Variant(
            "a{sv}", {"cwd": GLib.Variant("ay", str(tmp_path).encode() + b"\0")}
        ),
    )
    opened = []
    monkeypatch.setattr(app, "do_open", lambda files, count, hint: opened.extend(files))
    assert app.do_command_line(line) == 0
    assert [f.get_path() for f in opened] == [str(tmp_path / "relative.wav"), None]


def test_dropped_files_are_all_added(window, audio, tmp_path):
    from gi.repository import Gdk, Gio, Gtk

    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    files = Gdk.FileList.new_from_list(
        [
            Gio.File.new_for_path(str(audio)),
            Gio.File.new_for_path(str(second)),
            Gio.File.new_for_uri("bac-test://host/remote.wav"),
        ]
    )
    target = next(
        c for c in window.observe_controllers() if isinstance(c, Gtk.DropTarget)
    )
    assert target.emit("drop", files, 0, 0)
    pump(lambda: len(window.file_queue.files) == 2 and not window.file_queue.pending)
    assert [path for path, _message in window._test_errors] == [
        "bac-test://host/remote.wav"
    ]


def test_failed_trash_reports_a_trash_error(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    shown = []
    monkeypatch.setattr(window, "_show_message", lambda *args: shown.append(args))
    audio.unlink()  # Trash then fails: the file is already gone.
    queue._on_delete_response(None, "trash", str(audio))
    pump(lambda: shown)
    assert shown[0][0] == "Could not move the file to Trash"
    assert window._test_errors == []


def test_empty_queue_offers_adding_files(window, audio):
    queue = window.file_queue
    assert queue.stack.get_visible_child_name() == "empty"
    assert not window.visualizer_container.get_visible()
    assert not window.lookup_action("clear-queue").get_enabled()
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    assert queue.stack.get_visible_child_name() == "files"
    assert window.visualizer_container.get_visible()
    assert window.queue_label.get_label() == "1 file · 0:02"
    # Cuts are work: clearing them asks first.
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window.activate_action("win.clear-queue", None)
    assert queue.files == [str(audio)]
    window.visualizer.clear_all_markers()
    window.activate_action("win.clear-queue", None)
    assert queue.files == []
    assert queue.stack.get_visible_child_name() == "empty"
    assert not window.visualizer_container.get_visible()


def test_delete_removes_entry_and_keeps_file(window, audio, tmp_path):
    queue = window.file_queue
    second = tmp_path / "second.wav"
    second.write_bytes(audio.read_bytes())
    queue.add_files([str(audio), str(second)])
    pump(lambda: len(queue.files) == 2 and not queue.pending)
    pump(lambda: window.visualizer.duration > 0)
    window.visualizer.restore_markers([{"start": 0.2, "stop": 0.5}])
    window._update_cut_rows()
    assert queue.file_rows[0].get_subtitle().endswith(" · 1 segment")
    queue.file_rows[0].grab_focus()
    assert queue._remove_focused_row()
    assert queue.files == [str(second)]
    assert audio.is_file()
    assert queue.file_list.get_focus_child() is queue.file_rows[0]


def test_converting_locks_queue_and_shows_banner(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.activate_action("win.convert", None)
    assert window.converter.busy
    assert window.convert_button.get_label() == "Cancel"
    assert not window.options_scroll.get_sensitive()
    assert not window.lookup_action("add-files").get_enabled()
    assert not queue.file_rows[0].remove_button.get_sensitive()
    assert window.queue_label.get_label().startswith("Converting 1 of 1")
    pump(lambda: window.conversion.last_batch is not None)
    assert window.convert_button.get_label() == "Convert 1 file"
    assert window.options_scroll.get_sensitive()
    assert queue.file_rows[0].remove_button.get_sensitive()
    assert window.banner.get_revealed()
    assert window.banner.get_title() == "1 file converted"
    assert queue.file_rows[0].result_box.get_first_child() is not None
    assert window.queue_label.get_label() == "1 file · 0:02"


def test_cancelling_cleans_up_and_offers_retry(window, audio):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    window.format_row.set_selected(window._format_list.index("flac"))
    before = set(audio.parent.iterdir())
    window.activate_action("win.convert", None)
    window.activate_action("win.cancel-conversion", None)
    pump(lambda: window.conversion.last_batch is not None)
    assert window.conversion.last_batch.items[0].status == "cancelled"
    assert set(audio.parent.iterdir()) == before
    assert window.banner.get_title() == "1 file was not converted"
    assert window.banner.get_button_label() == "Try Again"


def test_late_track_expansion_keeps_shown_file(window, audio, tmp_path, monkeypatch):
    video = tmp_path / "two tracks.mka"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=duration=1"]
        + ["-f", "lavfi", "-i", "sine=frequency=500:duration=1"]
        + ["-map", "0", "-map", "1", str(video)],
        check=True,
    )
    queue = window.file_queue
    deliver = queue._probe_ready
    held = []

    # The video's tracks arrive after the plain file is already shown below it.
    def probe_ready(token, path, info, error):
        if path == str(video):
            held.append((token, path, info, error))
        else:
            deliver(token, path, info, error)

    monkeypatch.setattr(queue, "_probe_ready", probe_ready)
    queue.add_file(str(video))
    queue.add_file(str(audio))
    pump(lambda: held and queue.active_file_index is not None)
    assert queue.files[queue.active_file_index] == str(audio)
    deliver(*held[0])
    assert len(queue.files) == 3
    assert queue.files[queue.active_file_index] == str(audio)


def test_retry_uses_current_settings_and_queue(window, audio, monkeypatch):
    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.activate_action("win.convert", None)
    window.activate_action("win.cancel-conversion", None)
    pump(lambda: window.conversion.last_batch is not None)
    window.format_row.set_selected(window._format_list.index("mp3"))
    started = []
    monkeypatch.setattr(
        window.converter,
        "start_batch",
        lambda files, settings, *_: started.append((files, settings["format"])),
    )
    window.conversion.banner_activated()
    assert started == [((str(audio),), "mp3")]

    started.clear()
    window.conversion._banner_action = ("retry", (str(audio),))
    queue.remove_file(0)
    window.conversion.banner_activated()
    assert started == []
    assert not window.banner.get_revealed()


def test_marked_speech_exports_as_one_file(window, audio):
    import json
    import subprocess

    queue = window.file_queue
    queue.add_file(str(audio))
    pump(lambda: not queue.pending and window.visualizer.duration > 0)
    panel = window.pause_panel
    panel.length_buttons[0].set_active(True)
    panel._on_found(None, [-6.0] * 50 + [-80.0] * 100 + [-6.0] * 50)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    [item] = window.conversion.last_batch.items
    assert item.successful and len(item.outputs) == 1
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-of", "json", item.outputs[0]],
        capture_output=True,
        check=True,
        text=True,
    )
    # 0.65 s + 0.65 s of speech; the 0.7 s pause is gone.
    assert float(json.loads(probe.stdout)["format"]["duration"]) == pytest.approx(
        1.3, abs=0.05
    )


def test_trashed_source_leaves_the_queue(window, audio):
    queue = window.file_queue
    assert queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    window.format_row.set_selected(window._format_list.index("flac"))
    window.delete_originals_switch.set_active(True)
    window.on_convert(None)
    pump(lambda: window.conversion.last_batch is not None)
    [item] = window.conversion.last_batch.items
    assert item.successful and not item.original_kept
    assert not audio.exists() and Path(item.outputs[0]).is_file()
    assert queue.files == [] and queue.file_rows == []


def test_skip_trash_menu_deletes_from_row(window, audio):
    from gi.repository import GLib

    queue = window.file_queue
    assert queue.add_file(str(audio))
    pump(lambda: not queue.pending)
    row = queue.file_rows[0]
    assert row.delete_button.get_tooltip_text() == "Move to Trash"
    from conftest import DATA_HOME

    trash = Path(DATA_HOME) / "Trash" / "files"
    before = set(trash.iterdir()) if trash.is_dir() else set()
    action = window.get_application().lookup_action("skip-trash")
    action.change_state(GLib.Variant.new_boolean(True))
    try:
        assert window.app.config.get("skip_trash") is True
        assert row.delete_button.get_tooltip_text() == "Delete Permanently"
        assert "deleted permanently" in window.delete_box.get_tooltip_text()
        assert window._collect_conversion_settings()["skip_trash"]
        queue._on_delete_response(None, "trash", str(audio))
        pump(lambda: not audio.exists() and not queue.file_rows)
    finally:
        action.change_state(GLib.Variant.new_boolean(False))
    assert (set(trash.iterdir()) if trash.is_dir() else set()) == before
