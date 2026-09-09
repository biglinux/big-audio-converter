"""Real FFmpeg regression contracts for the product audit."""
import json
import subprocess
from pathlib import Path

import numpy as np
import pytest
from app.audio.converter import AudioConverter
from gi.repository import GLib


def converter():
    return AudioConverter()

from app.audio.waveform import WaveformGenerator


def run(argv):
    return subprocess.run(argv, check=True, capture_output=True, timeout=20)


def pcm(path):
    return np.frombuffer(run(['ffmpeg','-v','error','-i',str(path),'-map','0:a:0','-f','f32le','-']).stdout, dtype='<f4')


def probe(path):
    return json.loads(run(['ffprobe','-v','error','-show_streams','-show_format','-of','json',str(path)]).stdout)


@pytest.fixture
def tone(tmp_path):
    p = tmp_path/'tone.wav'
    run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=997:sample_rate=48000:duration=3','-metadata','title=Título 東京 🎵','-c:a','pcm_s16le','-y',str(p)])
    return p


def settings(path, **kwargs):
    return {'format':'wav','cut_enabled':True,'file_markers':{str(path):[
        {'start':.5,'stop':1.5,'start_str':'00:00:00.500','stop_str':'00:00:01.500'}]}, **kwargs}


def test_unmodified_flac_preserves_pcm(tone, tmp_path):
    out = tmp_path/'control.flac'
    assert converter().convert_file(str(tone),str(out),{'format':'flac'},lambda _:None)
    assert np.array_equal(pcm(tone),pcm(out))


def test_cut_respects_zero_volume(tone, tmp_path):
    out = tmp_path/'muted.wav'
    assert converter().convert_file(str(tone),str(out),settings(tone,volume=0))
    assert np.max(np.abs(pcm(out))) == 0


def test_cut_respects_double_speed(tone, tmp_path):
    out = tmp_path/'fast.wav'
    assert converter().convert_file(str(tone),str(out),settings(tone,speed=2))
    assert abs(len(pcm(out))-24000) <= 480  # atempo has algorithm-dependent end padding.


def test_segments_never_overwrite_unrelated_output(tone, tmp_path):
    target = tmp_path/'cut_segment1.wav'
    target.write_bytes(b'UNRELATED USER DATA')
    st = settings(tone,cut_merge=False)
    st['file_markers'][str(tone)].append({'start':1.5,'stop':2.5,'start_str':'1.5','stop_str':'2.5'})
    converter().convert_file(str(tone),str(tmp_path/'cut.wav'),st)
    assert target.read_bytes() == b'UNRELATED USER DATA'


def test_exhausted_numbered_names_do_not_select_existing_file(tone, tmp_path):
    (tmp_path/'tone.mp3').write_bytes(b'old')
    for i in range(1,101):
        (tmp_path/f'tone-converted-{i}.mp3').write_bytes(b'old')
    selected = converter()._get_output_path(str(tone),'mp3')
    assert not Path(selected).exists()


def test_all_rejected_segments_are_not_success(tone, tmp_path):
    st = settings(tone,cut_merge=False)
    st['file_markers'][str(tone)] = [{'start':0,'stop':0},{'start':1,'stop':1}]
    assert not converter().convert_file(str(tone),str(tmp_path/'none.wav'),st)


def test_merge_is_not_silent_partial_success(tone, tmp_path):
    st = settings(tone,cut_merge=True)
    st['file_markers'][str(tone)].append({'start':100,'stop':101,'start_str':'100','stop_str':'101'})
    assert not converter().convert_file(str(tone),str(tmp_path/'incomplete.wav'),st)


def test_real_filename_with_double_colon_works(tone, tmp_path):
    source = tmp_path/'real::name.wav'
    source.write_bytes(tone.read_bytes())
    assert converter().convert_file(str(source),str(tmp_path/'out.flac'),{'format':'flac'},lambda _:None)


def test_normalization_preserves_requested_original_rate(tone, tmp_path):
    out=tmp_path/'normalized.wav'
    assert converter().convert_file(str(tone),str(out),{'format':'wav','normalize':True},lambda _:None)
    assert probe(out)['streams'][0]['sample_rate'] == '48000'


def test_cut_retains_title_metadata(tone, tmp_path):
    out=tmp_path/'tagged.wav'
    assert converter().convert_file(str(tone),str(out),settings(tone))
    assert probe(out)['format'].get('tags',{}).get('title') == 'Título 東京 🎵'


def test_waveform_represents_right_channel(tmp_path):
    source=tmp_path/'right.wav'
    run(['ffmpeg','-v','error','-f','lavfi','-i','aevalsrc=0|0.25*sin(2*PI*997*t):s=48000:d=2','-y',str(source)])
    class Sink:
        markers_enabled=False
        data=None
        def set_loading(self,*args): pass
        def set_waveform(self,data,duration): self.data=data
        def queue_draw(self): pass
    sink=Sink()
    WaveformGenerator().generate(str(source),converter(),sink)
    context = GLib.MainContext.default()
    for _ in range(100):
        if not context.pending():
            break
        context.iteration(False)
    assert np.max(np.abs(sink.data['levels'][0])) > .01


def test_short_segments_produce_actual_outputs(tone, tmp_path):
    """10 ms cuts are now supported, not discarded under the old 100 ms limit."""
    st = settings(tone, cut_merge=False)
    st["file_markers"][str(tone)] = [{"start": 0, "stop": .01}, {"start": 1, "stop": 1.01}]
    conv = converter()
    assert conv.convert_file(str(tone), str(tmp_path / "short.wav"), st)
    assert len(conv.last_result.outputs) == 2
    for output in conv.last_result.outputs:
        assert len(pcm(output)) == 480
