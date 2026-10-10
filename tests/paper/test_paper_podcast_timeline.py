"""Actual PCM boundaries, injected pauses and non-measured audio admission."""
import io
import json
from pathlib import Path
import wave

import pytest

pytestmark = pytest.mark.unit


def wav(seconds, rate=8000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(rate)
        writer.writeframes(b'\0\0' * int(seconds * rate))
    return output.getvalue()


def test_segment_receipt_tracks_real_frames_chunk_pauses_and_section_breaks(monkeypatch):
    import lib.paper.podcast_engine._audio as audio
    from lib import tts
    monkeypatch.setattr(tts, 'max_input_chars', lambda: 6)
    durations = iter([.1, .2, .3, .4])
    monkeypatch.setattr(audio, '_synth_chunk_with_retry', lambda *_args, **_kwargs: (wav(next(durations)), 'unit-tts'))
    monkeypatch.setattr(audio, '_transcode_to_mp3', lambda _data: None)
    script = {'mode': 'short', 'segments': [
        {'section': 'method', 'text': 'abcdefghijkl', 'est_seconds': 60},
        {'section': 'method', 'text': 'second', 'est_seconds': 60},
        {'section': 'recap', 'text': 'third', 'est_seconds': 60},
    ]}
    result = audio.synthesize_script_audio(script, voice='unit', fmt='wav', max_workers=1)
    receipt = result['timelineV1']
    assert [row['startSec'] for row in receipt['segments']] == pytest.approx([0, .75, 1.85])
    assert [row['endSec'] for row in receipt['segments']] == pytest.approx([.45, 1.05, 2.25])
    assert receipt['durationSec'] == pytest.approx(tts.wav_duration(result['audio_bytes']))
    schema = json.loads((Path(__file__).resolve().parents[2] / 'contracts/paper_podcast_timeline_v1.schema.json').read_text())
    import jsonschema
    jsonschema.validate(receipt, schema)
    assert len(json.dumps(receipt).encode()) < 1024, 'bounded timing metadata for three segments'


def test_mp3_fallback_does_not_manufacture_measured_timing(monkeypatch):
    import lib.paper.podcast_engine._audio as audio
    monkeypatch.setattr(audio, '_synth_chunk_with_retry', lambda *_args, **_kwargs: (b'ID3' + b'\0' * 4096, 'unit-tts'))
    result = audio.synthesize_script_audio({'mode': 'short', 'segments': [
        {'section': 'recap', 'text': 'A short source explanation.', 'est_seconds': 60},
    ]}, voice='unit', fmt='mp3', max_workers=1)
    assert result['duration_estimated']
    assert not result.get('timelineV1')


def test_pcm_parameter_mismatch_withholds_source_timing(monkeypatch):
    import lib.paper.podcast_engine._audio as audio
    parts = iter([wav(.1), wav(.1, rate=16000)])
    monkeypatch.setattr(audio, '_synth_chunk_with_retry', lambda *_args, **_kwargs: (next(parts), 'unit-tts'))
    monkeypatch.setattr(audio, '_transcode_to_mp3', lambda _data: None)
    result = audio.synthesize_script_audio({'mode': 'short', 'segments': [
        {'section': 'method', 'text': 'First explanation.'},
        {'section': 'recap', 'text': 'Second explanation.'},
    ]}, voice='unit', fmt='wav', max_workers=1)
    assert result['timelineV1'] is None


def test_full_script_timing_metadata_stays_small_at_admitted_segment_limit(monkeypatch):
    import lib.paper.podcast_engine._audio as audio
    from lib.paper.podcast_prompts import PODCAST_SEGMENT_LIMITS
    monkeypatch.setattr(audio, '_synth_chunk_with_retry', lambda *_args, **_kwargs: (wav(.1), 'unit-tts'))
    monkeypatch.setattr(audio, '_transcode_to_mp3', lambda _data: None)
    count = PODCAST_SEGMENT_LIMITS['full']
    result = audio.synthesize_script_audio({'mode': 'full', 'segments': [
        {'section': 'method', 'text': 'A bounded explanation.'} for _ in range(count)
    ]}, voice='unit', fmt='wav', max_workers=1)
    receipt = result['timelineV1']
    assert len(receipt['segments']) == count
    size = len(json.dumps(receipt).encode())
    assert size < 8192
    print(f'PCM timeline measurement: {count} segments, {size} metadata bytes')


def test_all_podcast_segments_start_with_one_idle_hint_and_keep_order(monkeypatch):
    import threading
    import time
    from lib.paper.podcast_engine import _audio as audio
    from lib import tts
    monkeypatch.setattr(tts,'max_input_chars',lambda:4000)
    gate=threading.Barrier(8)
    def synth(text,**kwargs):
        gate.wait(timeout=3)
        index=int(text.split()[1].rstrip('.'))
        return wav(.1+.125*index),'unit-tts'
    monkeypatch.setattr(audio,'_synth_chunk_with_retry',synth)
    monkeypatch.setattr(audio,'_transcode_to_mp3',lambda data:None)
    segments=[{'section':f'part-{i}','text':f'Content {i}.'} for i in range(8)]
    began=time.monotonic()
    result=audio.synthesize_script_audio({'mode':'short','segments':segments},
        voice='unit',fmt='wav',max_workers=1)
    rows=result['timelineV1']['segments']
    assert [row['index'] for row in rows]==list(range(8))
    assert [row['endSec']-row['startSec'] for row in rows]==pytest.approx([.1+.125*i for i in range(8)])
    assert not [t for t in threading.enumerate() if t.name.startswith('paper-podcast-tts_')]
    print(f'podcast-demand segments=8 idle_hint=1 settled_ms={(time.monotonic()-began)*1000:.2f} retained_threads=0')
