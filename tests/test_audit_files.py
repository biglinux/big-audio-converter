"""No-clobber, filename and concurrent-publication regressions."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shutil

import pytest
from test_audit_audio import tone, options, pcm
from app.audio.converter import AudioConverter


def test_more_than_100_collisions(tone, tmp_path):
    (tmp_path / "source.mp3").write_bytes(b"keep")
    for index in range(1, 151):
        (tmp_path / f"source-converted-{index}.mp3").write_bytes(b"keep")
    result = AudioConverter()._get_output_path(str(tone), "mp3")
    assert not Path(result).exists()


def test_separate_segments_preserve_existing_files(tone, tmp_path):
    protected = tmp_path / "cut_segment1.wav"
    protected.write_bytes(b"Do not replace this file")
    before = protected.read_bytes()
    converter = AudioConverter()
    markers = [{"start": 0, "stop": 0.5}, {"start": 1, "stop": 1.5}]
    assert converter.convert_file(str(tone), str(tmp_path / "cut.wav"), options(tone, cut_merge=False, file_markers={str(tone): markers}))
    assert protected.read_bytes() == before
    assert len(converter.last_result.outputs) == 2
    assert all(len(pcm(path)) == 24000 for path in converter.last_result.outputs)


@pytest.mark.parametrize("name", ["real::name.wav", "space name.wav", "a'[]()&$;.wav", "line\nbreak.wav", "音声 😀.wav", "صوت.wav"])
def test_valid_local_filenames(tone, tmp_path, name):
    source = tmp_path / name
    shutil.copyfile(tone, source)
    output = tmp_path / "converted.flac"
    assert AudioConverter().convert_file(str(source), str(output), {"format": "flac"})
    assert output.is_file()


def test_dangling_symlink_is_not_replaced(tone, tmp_path):
    output = tmp_path / "output.flac"
    output.symlink_to(tmp_path / "missing-target")
    converter = AudioConverter()
    assert converter.convert_file(str(tone), str(output), {"format": "flac"})
    assert output.is_symlink()
    assert converter.last_result.outputs[0] != str(output)
    assert not (tmp_path / "missing-target").exists()


def test_concurrent_exports_get_distinct_outputs(tone, tmp_path):
    output = tmp_path / "shared.flac"
    def convert(_):
        converter = AudioConverter()
        assert converter.convert_file(str(tone), str(output), {"format": "flac"})
        return converter.last_result.outputs[0]
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(convert, range(4)))
    assert len(set(paths)) == 4
    assert all(Path(path).is_file() for path in paths)
    assert not list(tmp_path.glob(".bac-*"))
