"""Moving originals to the Trash once everything made from them converted."""

import os

import pytest
from app.audio.converter import AudioConverter, trash_originals
from app.audio.media import ConversionResult, file_identity, track_identifier
from conftest import DATA_HOME, command, convert_batch


def tone(path):
    command(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-ar", "48000", str(path),
    )  # fmt: skip
    return path


@pytest.fixture
def source(tmp_path):
    return tone(tmp_path / "source.wav")


def trashed(name):
    return os.path.exists(os.path.join(DATA_HOME, "Trash", "files", name))


def two_track_video(tmp_path, source):
    video = tmp_path / "tracks.mka"
    command(
        "ffmpeg", "-v", "error", "-i", str(source), "-f", "lavfi",
        "-i", "aevalsrc=0.25:s=48000:d=2", "-map", "0:a:0", "-map", "1:a:0",
        "-c:a", "flac", str(video),
    )  # fmt: skip
    return video, {
        track_identifier(video, index): {
            "source_video": str(video),
            "track_index": index,
            "output_name": f"tracks-track{index + 1}.flac",
            "track_count": 2,
        }
        for index in (0, 1)
    }


def test_original_goes_to_trash_after_success(tmp_path):
    original = tone(tmp_path / "moved.wav")
    [item] = convert_batch(
        [str(original)], {"format": "flac", "delete_originals": True}
    )
    assert item.successful and not item.original_kept
    assert item.original == "The original was moved to the Trash."
    assert not original.exists() and trashed("moved.wav")
    assert os.path.getsize(item.outputs[0]) > 0


def test_originals_are_kept_by_default(source):
    [item] = convert_batch([str(source)], {"format": "flac"})
    assert item.successful and item.original == ""
    assert source.exists()


def test_failed_track_keeps_its_video(tmp_path, source):
    video, metadata = two_track_video(tmp_path, source)
    first, second = metadata
    metadata[second]["track_index"] = 7
    items = convert_batch(
        [first, second],
        {"format": "flac", "track_metadata": metadata, "delete_originals": True},
    )
    assert [item.status for item in items] == ["success", "failed"]
    assert items[0].original_kept and "was not converted" in items[0].original
    assert video.exists() and not trashed("tracks.mka")


def test_partial_track_batch_keeps_its_video(tmp_path, source):
    video, metadata = two_track_video(tmp_path, source)
    [item] = convert_batch(
        [next(iter(metadata))],
        {"format": "flac", "track_metadata": metadata, "delete_originals": True},
    )
    assert item.successful and item.original_kept
    assert "Not every audio track" in item.original
    assert video.exists()


def test_removed_track_keeps_its_video(tmp_path, source):
    video, metadata = two_track_video(tmp_path, source)
    first, second = metadata
    del metadata[second]
    [item] = convert_batch(
        [first],
        {"format": "flac", "track_metadata": metadata, "delete_originals": True},
    )
    assert item.successful and item.original_kept
    assert "Not every audio track" in item.original
    assert video.exists()


def test_cut_file_keeps_its_original(tmp_path):
    original = tone(tmp_path / "cut.wav")
    [item] = convert_batch(
        [str(original)],
        {
            "format": "flac",
            "file_markers": {str(original): [{"start": 0.2, "stop": 0.6}]},
            "delete_originals": True,
        },
    )
    assert item.successful and item.original_kept
    assert "only part of the original" in item.original
    assert original.exists()


def test_video_keeps_its_picture(tmp_path, source):
    video = tmp_path / "clip.mkv"
    command(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=s=64x48:d=1",
        "-i", str(source), "-map", "0:v", "-map", "1:a", "-c:v", "ffv1",
        "-c:a", "flac", "-shortest", str(video),
    )  # fmt: skip
    [item] = convert_batch([str(video)], {"format": "flac", "delete_originals": True})
    assert item.successful and item.original_kept
    assert video.exists()


def test_subtitles_keep_their_original(tmp_path, source):
    subtitles = tmp_path / "words.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n")
    original = tmp_path / "spoken.mka"
    command(
        "ffmpeg", "-v", "error", "-i", str(source), "-i", str(subtitles),
        "-map", "0:a", "-map", "1:s", "-c:a", "flac", "-c:s", "srt", str(original),
    )  # fmt: skip
    [item] = convert_batch(
        [str(original)], {"format": "flac", "delete_originals": True}
    )
    assert item.successful and item.original_kept
    assert original.exists()


def test_entries_must_see_one_original(tmp_path):
    original = tone(tmp_path / "swapped.wav")
    output = tmp_path / "out.flac"
    output.write_bytes(b"converted")
    seen = file_identity(str(original))
    items = [
        ConversionResult(
            str(original),
            "success",
            (str(output),),
            originals=((str(original), identity, True),),
        )
        for identity in (seen, (0, 0, 0, 0))
    ]
    kept = trash_originals(items, None)
    assert all(item.original_kept for item in kept)
    assert original.exists()


def test_every_track_converted_trashes_the_video(tmp_path, source):
    video, metadata = two_track_video(tmp_path, source)
    items = convert_batch(
        list(metadata),
        {"format": "flac", "track_metadata": metadata, "delete_originals": True},
    )
    assert all(item.successful and not item.original_kept for item in items)
    assert not video.exists() and trashed("tracks.mka")


def test_linked_original_is_never_trashed(tmp_path, source):
    link = tmp_path / "link.wav"
    link.symlink_to(source)
    [item] = convert_batch([str(link)], {"format": "flac", "delete_originals": True})
    assert item.successful and item.original_kept
    assert link.is_symlink() and source.exists()


def test_trash_failure_keeps_the_original(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from gi.repository import GLib

    def refuse(_cancellable):
        raise GLib.Error("no Trash on this device")

    original = tone(tmp_path / "kept.wav")
    fake = SimpleNamespace(new_for_path=lambda _path: SimpleNamespace(trash=refuse))
    monkeypatch.setattr("app.audio.converter.Gio.File", fake)
    [item] = convert_batch(
        [str(original)], {"format": "flac", "delete_originals": True}
    )
    assert item.successful and item.original_kept
    assert item.original == "The original could not be moved to the Trash and was kept."
    assert original.exists()


def test_joined_sources_all_go_to_trash(tmp_path):
    first, second = tone(tmp_path / "first.wav"), tone(tmp_path / "second.wav")
    items = convert_batch(
        [str(first), str(second)],
        {"format": "flac", "join_files": True, "delete_originals": True},
    )
    assert [item.original_kept for item in items] == [False, False]
    assert not first.exists() and not second.exists()


def test_cancelled_batch_keeps_originals(tmp_path):
    original = tone(tmp_path / "cancelled.wav")
    engine = AudioConverter()
    worker = engine.start_batch(
        [str(original)],
        {"format": "flac", "delete_originals": True},
        None,
        lambda: None,
    )
    engine.cancel_conversion()
    worker.join(30)
    [item] = engine.last_batch_result.items
    engine.cleanup()
    assert not item.successful and original.exists()


def test_skip_trash_deletes_the_original(tmp_path):
    original = tone(tmp_path / "gone.wav")
    [item] = convert_batch(
        [str(original)],
        {"format": "flac", "delete_originals": True, "skip_trash": True},
    )
    assert item.successful and not item.original_kept
    assert item.original == "The original was deleted."
    assert not original.exists() and not trashed("gone.wav")
    assert os.path.getsize(item.outputs[0]) > 0
