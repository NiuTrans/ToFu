"""Shared codec doubles for motion and paper progress workflow tests.

Entry: install_motion_media_doubles; write_fake_video seeds expected output.
Probe metadata follows scene HTML or the owning film sidecar/timeline. The
real worker and media gates run; expensive codecs and author prewarm are ports.
"""
import json
import os
from pathlib import Path
import re

from lib import motion_video as mv


def install_motion_media_doubles(monkeypatch):
    def fake_render(project_dir, output, **kw):
        with open(output, 'wb') as f:
            f.write(b'mp4')
        return {'ok': True, 'output': output, 'elapsed': 0.1,
                'render_time_s': 0.1, 'category': '', 'detail': ''}
    monkeypatch.setattr('lib.motion_video.render_project', fake_render)

    def fake_probe(path, **kw):
        idx = os.path.join(os.path.dirname(path), 'index.html')
        dur = 4.0
        has_audio = False
        if os.path.isfile(idx):
            m = re.search(r'data-duration="([0-9.]+)"',
                          open(idx, encoding='utf-8').read())
            if m:
                dur = float(m.group(1))
        else:
            # A revision probes a private candidate below the owning job.
            # Report the assembled timeline, not a default scene duration.
            job = next((parent for parent in Path(path).parents
                        if (parent / 'scenes.json').is_file()), Path(path).parent)
            sidecar = job / 'final.srt'
            if os.path.isfile(sidecar):
                with open(sidecar, encoding='utf-8') as stream:
                    start, end = mv.total_span(mv.parse_srt(stream.read()))
                dur = end - start
            elif (job / 'scenes.json').is_file():
                scenes = json.loads((job / 'scenes.json').read_text())
                dur = sum(float(scene.get('content_duration_s') or
                                (scene['end'] - scene['start'])) for scene in scenes)
            has_audio = any(os.path.isfile(os.path.join(job, name))
                            for name in ('audio/narration.wav', 'audio_plan.json'))
        return {'codec': 'h264', 'width': 1080, 'height': 1440, 'fps': 30.0,
                'duration': dur, 'has_audio': has_audio}
    monkeypatch.setattr('lib.motion_video.probe_video', fake_probe)
    monkeypatch.setattr(
        'lib.motion_video.concat_mp4s',
        lambda inputs, output, **kw: write_fake_video(output) or {
            'ok': True, 'output': output, 'duration': 8.0, 'mode': 'copy',
            'elapsed': 0.1})
    monkeypatch.setattr(
        'lib.motion_video.mux_audio_video',
        lambda v, a, output, **kw: write_fake_video(output) or {
            'ok': True, 'output': output, 'duration': 8.0, 'elapsed': 0.1})
    # Engine concurrency tests exercise the scheduling boundary, not network
    # installation of the shared craft/font dependencies.
    monkeypatch.setattr(
        'lib.motion_video._scene_author.prepare_parallel_author_dependencies',
        lambda **kwargs: True)



def write_fake_video(path):
    with open(path, 'wb') as f:
        f.write(b'mp4')
    return None

