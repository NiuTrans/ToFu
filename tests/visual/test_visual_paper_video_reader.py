"""One film, real media controls, normalized chapters and honest scene revisions."""
import importlib
import json
from pathlib import Path
import subprocess

import pytest
from tests.visual.test_visual_surfaces import _wait_ready
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual


@pytest.fixture(autouse=True)
def warm_catalog():
    importlib.import_module('lib.llm_dispatch.config')


def test_video_chapters_preview_revision_and_source_switch(page, tmp_path, assert_no_js_errors):
    fitz = pytest.importorskip('pymupdf')
    from lib.motion_video._env import ffmpeg_bin
    ffmpeg = ffmpeg_bin()
    if not ffmpeg:
        pytest.skip('ffmpeg is required for a real browser-playable MP4 fixture')
    source = tmp_path / 'Measured methods and evidence.pdf'
    document = fitz.open(); document.new_page().insert_text((42, 72), 'Methods, controlled comparisons and limits.')
    source.write_bytes(document.tobytes()); document.close()
    movie = tmp_path / 'film.mp4'
    encoded = subprocess.run([ffmpeg, '-y', '-loglevel', 'error', '-f', 'lavfi', '-i',
        'color=c=0x181722:s=640x360:r=10',
        '-t', '12', '-c:v', 'libx264', '-threads', '1', '-g', '10', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(movie)],
        capture_output=True, timeout=30)
    assert encoded.returncode == 0, encoded.stderr.decode(errors='replace')
    movie_bytes = movie.read_bytes()
    scene_rows = [
        {'scene_id': 'claim', 'on_screen': 'Start with the research question', 'text': 'The paper asks a question under explicit assumptions.'},
        {'scene_id': 'evidence', 'on_screen': 'Inspect the controlled comparison', 'text': 'A controlled comparison tests the claim against a baseline.'},
        {'scene_id': 'limits', 'on_screen': 'Check limits before applying the claim', 'text': 'The result does not establish generalization beyond the stated setting.'},
    ]
    for index, row in enumerate(scene_rows):
        row.update(has_video=True, timeline_contract_version='motion-timeline-v1',
            timeline_start_s=index * 4, timeline_end_s=(index + 1) * 4, start=index * 2, end=(index + 1) * 2)
    cached = {'ok': True, 'found': True, 'running': False, 'task_id': 'film-fixture', 'model': 'fixture-model',
              'result': {'duration': 12, 'narrated': True}, 'artifact_quality': {'degraded': False}}
    questions = []; regens = []; starts = []; films = []
    revision_applied = False
    page.on('request', lambda request: starts.append(request.url) if '/paper/video/start' in request.url else None)
    page.route('**/api/v4/motion/status', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'tts_available': True})))
    page.route('**/api/v4/paper/video/lookup?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps(cached)))
    page.route('**/api/v4/motion/videos/film-fixture/scenes', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'scenes': scene_rows})))
    def media_response(route):
        # Match the real file route's Range contract so native seeking settles.
        interval = route.request.headers.get('range', '')
        headers = {'Accept-Ranges': 'bytes'}
        if interval.startswith('bytes='):
            start, end = interval[6:].split('-', 1)
            start = int(start or '0'); end = min(int(end) if end else len(movie_bytes) - 1, len(movie_bytes) - 1)
            headers['Content-Range'] = f'bytes {start}-{end}/{len(movie_bytes)}'
            route.fulfill(status=206, headers=headers, content_type='video/mp4', body=movie_bytes[start:end + 1])
        else:
            route.fulfill(headers=headers, content_type='video/mp4', body=movie_bytes)
    def film(route):
        films.append(route.request.url)
        media_response(route)
    page.route('**/api/v4/motion/videos/film-fixture/file*', film)
    page.route('**/api/v4/motion/videos/film-fixture/scenes/*/file*', media_response)
    def regen(route):
        regens.append(route.request.url)
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'task_id': 'revision-fixture'}))
    page.route('**/api/v4/motion/videos/film-fixture/scenes/*/regen', regen)
    page.route('**/api/v4/motion/videos/poll/revision-fixture?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({
        'data': {'done': True, 'status': 'done', 'result': {'applied': revision_applied}, 'events': [],
                 'cursor': {'requested': 0, 'next': 0, 'reset': False}}, 'meta': {}})))
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'report': '', 'lang': 'en'})))
    def qa_start(route):
        questions.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({'ok': True, 'task_id': 'video-question', 'paper_hash': questions[-1]['paper_hash']}))
    page.route('**/api/v4/paper/qa/start', qa_start)
    page.route('**/api/v4/paper/qa/poll?*', lambda route: route.fulfill(content_type='application/json', body=json.dumps({
        'ok': True, 'status': 'done', 'done': True, 'answer': 'The comparison is conditional.', 'events': [],
        'cursor': {'requested': 0, 'next': 0, 'reset': False}})))
    _wait_ready(page); page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible')
    page.locator('#paperLibraryList .paper-lib-loading').wait_for(state='detached')
    with page.expect_response(lambda response: '/paper/upload' in response.url) as uploaded:
        page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    assert uploaded.value.ok
    page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    _dismiss_onboarding_modals(page); page.locator('[data-paper-tab="video"]').click()
    page.locator('.paper-video-reader .paper-studio-chapter').nth(2).wait_for()
    page.wait_for_function("document.querySelector('#paperVideoPlayer').readyState >= 1")
    assert page.locator('video').count() == 1, 'one permanent player, no eager scene previews'
    page.locator('.paper-studio-chapter-play').nth(1).click()
    page.wait_for_function("document.querySelector('#paperVideoPlayer').currentTime >= 4 && !document.querySelector('#paperVideoPlayer').paused")
    page.locator('#paperVideoPlayer').evaluate('(element) => element.pause()')
    page.locator('.paper-studio-segment.is-active[data-scene="evidence"]').wait_for()
    page.locator('.paper-studio-chapter').nth(1).locator('button').nth(2).click()
    page.locator('dialog[open]').wait_for(); assert page.locator('video').count() == 2
    page.locator('dialog[open] .ui-dialog__footer button').nth(1).click()
    page.locator('dialog').wait_for(state='detached')
    page.wait_for_function("!!document.querySelector('.paper-studio-chapters .ui-alert')")
    assert len(regens) == 1
    assert not any('?v=' in url for url in films), 'unadopted revision keeps the current film'
    assert page.locator('video').count() == 1
    # Adoption refreshes the same player and restores its measured position.
    revision_applied = True
    page.locator('#paperVideoPlayer').evaluate('''(element) => {
      window.__videoEvents = [];
      for (const event of ['emptied', 'loadedmetadata', 'seeking', 'seeked', 'pause'])
        element.addEventListener(event, () => window.__videoEvents.push([event,element.currentTime,element.src]));
      element.currentTime = 4.25; element.playbackRate = 1.25;
    }''')
    page.wait_for_function("!document.querySelector('#paperVideoPlayer').seeking && Math.abs(document.querySelector('#paperVideoPlayer').currentTime - 4.25) < .15")
    page.locator('.paper-studio-chapter').nth(1).locator('button').nth(2).click()
    page.locator('dialog[open]').wait_for()
    page.locator('dialog[open] .ui-dialog__footer button').nth(1).click()
    page.locator('dialog').wait_for(state='detached')
    try:
        page.wait_for_function("document.querySelector('#paperVideoPlayer').src.includes('?v=') && document.querySelector('#paperVideoPlayer').readyState >= 3 && !document.querySelector('#paperVideoPlayer').seeking && Math.abs(document.querySelector('#paperVideoPlayer').currentTime - 4.25) < .15")
    except Exception:
        print('Video revision diagnostic:', page.locator('#paperVideoPlayer').evaluate('(element) => ({src:element.src,time:element.currentTime,ready:element.readyState,error:element.error?.message})'), films, regens,
              page.locator('.paper-studio-chapters').inner_text(), page.evaluate('window.__videoEvents'))
        raise
    assert page.locator('#paperVideoPlayer').evaluate('(element) => element.playbackRate') == 1.25
    assert '1.25' in page.locator('.paper-studio-player .ui-dropdown button').first.inner_text()
    assert not page.locator('.paper-studio-feedback').inner_text()
    assert len(regens) == 2 and page.locator('video').count() == 1
    artifacts = Path('/tmp/tofu-paper-video-previews'); artifacts.mkdir(exist_ok=True)
    host = page.locator('#paperVideoContent')
    for theme in ['light', 'tofu', 'dark']:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            if name == 'mobile': page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
            assert not host.evaluate('(element) => element.scrollWidth > element.clientWidth + 1')
            host.evaluate('(element) => element.scrollTop = 0')
            host.screenshot(path=str(artifacts / f'video-{theme}-{name}.png'), animations='disabled')
            host.evaluate('(element) => element.scrollTop = element.scrollHeight')
            if name == 'mobile':
                footer = page.locator('.paper-video-reader footer').bounding_box()
                switcher = page.locator('.paper-mobile-switch').bounding_box()
                assert footer['y'] + footer['height'] <= switcher['y'] + 1
            host.screenshot(path=str(artifacts / f'video-narration-{theme}-{name}.png'), animations='disabled')
    page.locator('.paper-studio-chapter-play').nth(1).click()
    page.locator('.paper-studio-chapter').nth(1).locator('button').nth(1).click()
    page.locator('.paper-qa-assistant').get_by_text('The comparison is conditional.', exact=True).wait_for()
    assert len(questions) == 1 and 'controlled comparison' in questions[0]['question']
    assert page.locator('#paperVideoPlayer').evaluate('(element) => !element.paused')
    page.locator('[data-paper-tab="video"]').click()
    assert page.locator('#paperVideoPlayer').evaluate('(element) => !element.paused'), 'returning to the same film preserves playback'
    page.locator('.paper-studio-chapter').first.locator('button').nth(2).click()
    page.locator('dialog[open]').wait_for()
    document = fitz.open(); document.new_page().insert_text((42, 72), 'A different paper must release the previous film.')
    other_bytes = document.tobytes(); document.close()
    page.locator('#paperModeContainer').evaluate("""(element, bytes) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(bytes)], 'other-paper.pdf', {type: 'application/pdf'}));
      element.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
    }""", list(other_bytes))
    page.locator('#paperVideoPlayer').wait_for(state='detached'); page.locator('dialog').wait_for(state='detached')
    assert not starts, 'reading a cached film never starts generation'
