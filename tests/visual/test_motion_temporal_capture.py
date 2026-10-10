"""Real Chromium evidence for absolute scene-time seeking and failed capture."""

from __future__ import annotations

import base64
import os

import pytest
from PIL import Image

from lib.design_sys.temporal_qa import screenshot_timeline_contact_sheet

pytestmark = pytest.mark.visual


def composition(tmp_path, *, timeline=True, throws=False):
    # The test timeline has a shorter animation span than its render duration.
    # At 94% animation progress the frame is red; at 9.4s it must be green.
    script = """
      window.__timelines = { main: {
        pause() { return this; },
        progress(p) { return this.totalTime(p * 2, false); },
        totalTime(seconds, suppressEvents) {
          if (suppressEvents) throw new Error('counter callback suppressed');
          document.body.style.background = seconds >= 9 ? '#00ff00' : '#ff0000';
          return this;
        }
      }};
    """ if timeline else ''
    if throws:
        script = script.replace('if (suppressEvents)', "throw new Error('seek failed'); if (suppressEvents)")
    (tmp_path / 'index.html').write_text(
        '<html><body style="margin:0;background:red">'
        '<div data-composition-id="main" data-duration="10" '
        'data-width="100" data-height="100"></div>'
        f'<script>{script}</script></body></html>')


def test_capture_uses_render_seconds_and_runs_counter_callbacks(tmp_path):
    composition(tmp_path)
    output = tmp_path / 'sheet.png'
    screenshot_timeline_contact_sheet(str(tmp_path), str(output), progresses=[0.1, 0.94])
    with Image.open(output) as sheet:
        assert sheet.getpixel((50, 90)) == (255, 0, 0)
        assert sheet.getpixel((162, 90)) == (0, 255, 0)


@pytest.mark.parametrize('timeline,throws', [(False, False), (True, True)])
def test_missing_or_broken_timeline_cannot_publish_evidence(tmp_path, timeline, throws):
    composition(tmp_path, timeline=timeline, throws=throws)
    output = tmp_path / 'sheet.png'
    output.write_bytes(b'previous evidence')
    with pytest.raises(Exception, match='registered|seek failed'):
        screenshot_timeline_contact_sheet(str(tmp_path), str(output))
    assert output.read_bytes() == b'previous evidence'


def test_explicit_duration_must_match_the_rendered_composition(tmp_path):
    composition(tmp_path)
    with pytest.raises(Exception, match='duration differs'):
        screenshot_timeline_contact_sheet(str(tmp_path), str(tmp_path / 'sheet.png'), duration_s=5)


def test_capture_seeks_real_video_footage_with_its_overlay(tmp_path):
    import chromium_env
    from playwright.sync_api import sync_playwright

    chromium_env.ensure_chromium_env(os.environ)

    # Encode a tiny local fixture with Chromium itself; no network, ffmpeg
    # installation, host media, or model is needed in this test.
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:
            page = browser.new_page()
            encoded = page.evaluate("""async () => {
              const canvas = document.createElement('canvas');
              canvas.width = canvas.height = 64;
              const ctx = canvas.getContext('2d');
              const stream = canvas.captureStream(20);
              const recorder = new MediaRecorder(stream, {mimeType: 'video/webm'});
              const chunks = [];
              recorder.ondataavailable = e => chunks.push(e.data);
              const stopped = new Promise(resolve => recorder.onstop = resolve);
              let color = 'red';
              const paint = () => { ctx.fillStyle = color; ctx.fillRect(0, 0, 64, 64); };
              paint();
              const drawing = setInterval(paint, 40);
              recorder.start();
              await new Promise(resolve => setTimeout(resolve, 600));
              color = 'lime'; paint();
              await new Promise(resolve => setTimeout(resolve, 600));
              recorder.stop(); await stopped; clearInterval(drawing);
              for (const track of stream.getTracks()) track.stop();
              const bytes = new Uint8Array(await new Blob(chunks).arrayBuffer());
              return btoa(String.fromCharCode(...bytes));
            }""")
        finally:
            browser.close()
    (tmp_path / 'clip.webm').write_bytes(base64.b64decode(encoded))
    (tmp_path / 'index.html').write_text(
        '<html><body style="margin:0"><div data-composition-id="main" '
        'data-duration="1" data-width="64" data-height="64">'
        '<video src="clip.webm" preload="auto" width="64" height="64"></video></div>'
        '<script>window.__timelines={main:{pause(){},totalTime(){}}};</script></body></html>')
    output = tmp_path / 'sheet.png'
    screenshot_timeline_contact_sheet(str(tmp_path), str(output), progresses=[0.1, 0.95])
    with Image.open(output) as sheet:
        first, last = sheet.getpixel((32, 74)), sheet.getpixel((108, 74))
        assert first[0] > 200 and first[1] < 20
        assert last[1] > 200 and last[0] < 20
