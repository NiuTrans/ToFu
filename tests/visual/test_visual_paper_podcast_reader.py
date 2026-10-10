"""Native media playback, measured chapter seeks, Q&A delegation and source teardown."""
import importlib
import io
import json
from pathlib import Path
import wave

import pytest
from tests.visual.test_visual_surfaces import _wait_ready
from tests.conftest import _dismiss_onboarding_modals

pytestmark = pytest.mark.visual


@pytest.fixture(autouse=True)
def warm_catalog():
    importlib.import_module('lib.llm_dispatch.config')


def test_podcast_artifact_chapters_playback_and_paper_questions(page, tmp_path, assert_no_js_errors):
    fitz = pytest.importorskip('pymupdf')
    source = tmp_path / 'podcast-paper.pdf'
    document = fitz.open()
    document.new_page().insert_text((42, 72), 'Measured methods and controlled evidence in paper reading.')
    source.write_bytes(document.tobytes()); document.close()
    output = io.BytesIO()
    with wave.open(output, 'wb') as writer:
        writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(8000)
        writer.writeframes(b'\0\0' * 8000 * 12)
    audio_bytes = output.getvalue()
    segments = [
        {'section': 'cold_open', 'text': 'The paper improves a measured outcome under stated assumptions.'},
        {'section': 'method', 'text': 'A controlled experiment compares the proposed method with a baseline.'},
        {'section': 'weaknesses', 'text': 'The result is conditional. Generalization remains unproven.'},
        {'section': 'recap', 'text': 'Check the research question, the experiment and the limits before applying the claim.'},
    ]
    receipt = {'format': 'paper.podcast.timeline.v1', 'basis': 'measured_pcm', 'durationSec': 12,
               'segments': [{'index': i, 'startSec': start, 'endSec': end}
                            for i, (start, end) in enumerate([(0, 2.45), (3.25, 5.7), (6.5, 8.7), (9.5, 12)])]}
    cached = {'ok': True, 'found': True, 'cached': True, 'scriptOnly': False, 'durationSec': 12,
              'model': 'fixture-model', 'audioUrl': '/api/paper/audio/fixture.wav',
              'script': {'title': 'Understand the claim, then inspect the evidence', 'segments': segments},
              'meta': {'source_kind': 'report_en', 'container': 'wav', 'timelineV1': receipt}}
    starts = []; questions = []
    page.on('request', lambda request: starts.append(request.url) if '/podcast/start' in request.url else None)
    page.route('**/api/v4/paper/podcast/status', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True, 'tts_available': True})))
    page.route('**/api/v4/paper/podcast/lookup', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps(cached)))
    page.route('**/api/paper/audio/fixture.wav', lambda route: route.fulfill(content_type='audio/wav', body=audio_bytes))
    page.route('**/api/v4/paper/report/lookup', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True, 'report': '', 'lang': 'en'})))

    def qa_start(route):
        questions.append(route.request.post_data_json)
        route.fulfill(content_type='application/json', body=json.dumps({
            'ok': True, 'task_id': 'studio-question', 'paper_hash': questions[-1]['paper_hash'],
        }))

    page.route('**/api/v4/paper/qa/start', qa_start)
    page.route('**/api/v4/paper/qa/poll?*', lambda route: route.fulfill(
        content_type='application/json', body=json.dumps({'ok': True, 'status': 'done', 'done': True,
            'answer': 'The source evidence is conditional.', 'events': [],
            'cursor': {'requested': 0, 'next': 0, 'reset': False}})))
    _wait_ready(page)
    page.locator('#paperModeBtn').click()
    page.wait_for_selector('#paperModeContainer', state='visible')
    page.locator('#paperLibraryList .paper-lib-loading').wait_for(state='detached')
    with page.expect_response(lambda response: '/paper/upload' in response.url) as uploaded:
        page.set_input_files('.paper-upload-btn input[type="file"]', str(source))
    assert uploaded.value.ok
    page.wait_for_selector('#paperPdfViewer .paper-reader-page canvas', state='attached', timeout=30000)
    _dismiss_onboarding_modals(page)
    page.locator('[data-paper-tab="podcast"]').click()
    page.locator('.paper-studio-reader').wait_for()
    page.wait_for_function("document.querySelector('#podcastAudio').readyState >= 1")
    assert page.locator('.paper-studio-chapter').count() == 4
    assert page.locator('.paper-studio-segment').count() == 4
    page.locator('.paper-studio-chapter-play').nth(1).click()
    page.wait_for_function("Math.abs(document.querySelector('#podcastAudio').currentTime - 3.25) < .1")
    page.locator('.paper-studio-segment.is-active[data-seg="1"]').wait_for()
    page.locator('.paper-studio-player .ui-dropdown').first.locator('button').first.click()
    page.get_by_role('menuitem', name='1.5×', exact=True).click()
    assert page.locator('#podcastAudio').evaluate('(el) => el.playbackRate') == 1.5
    assert not page.locator('.paper-studio-player [role="menu"]').first.is_visible()
    assert not page.locator('.paper-studio-player [role="menu"]').nth(1).is_visible()
    artifacts = Path('/tmp/tofu-paper-studio-previews'); artifacts.mkdir(exist_ok=True)
    host = page.locator('#paperPodcastContent')
    for theme in ['light', 'tofu', 'dark']:
        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
        for width, height, name in [(1440, 1000, 'desktop'), (390, 844, 'mobile')]:
            page.set_viewport_size({'width': width, 'height': height})
            if name == 'mobile':
                page.locator('.paper-mobile-switch-btn[data-view="reader"]').click()
            assert not host.evaluate('(el) => el.scrollWidth > el.clientWidth + 1')
            host.evaluate('(el) => el.scrollTop = 0')
            host.screenshot(path=str(artifacts / f'podcast-{theme}-{name}.png'), animations='disabled')
            host.evaluate('(el) => el.scrollTop = el.scrollHeight')
            if name == 'mobile':
                footer = page.locator('.paper-studio-reader footer').bounding_box()
                bar = page.locator('.paper-mobile-switch').bounding_box()
                assert footer['y'] + footer['height'] <= bar['y'] + 1
            host.screenshot(path=str(artifacts / f'podcast-transcript-{theme}-{name}.png'), animations='disabled')
    page.locator('#podcastAudio').evaluate('(el) => el.play()')
    page.wait_for_function("!document.querySelector('#podcastAudio').paused")
    page.locator('.paper-studio-chapter').nth(1).locator('button').nth(1).click()
    page.locator('.paper-qa-assistant').get_by_text('The source evidence is conditional.', exact=True).wait_for()
    assert len(questions) == 1 and 'controlled experiment' in questions[0]['question']
    assert questions[0]['paper_hash']
    assert page.locator('#podcastAudio').evaluate('(el) => !el.paused'), 'same-source Q&A should allow continued listening'
    document = fitz.open(); document.new_page().insert_text((42, 72), 'A different source must stop prior playback.')
    other = tmp_path / 'other-paper.pdf'; other.write_bytes(document.tobytes()); document.close()
    page.locator('#paperModeContainer').evaluate("""(element, bytes) => {
      const transfer = new DataTransfer();
      transfer.items.add(new File([new Uint8Array(bytes)], 'other-paper.pdf', {type: 'application/pdf'}));
      element.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
    }""", list(other.read_bytes()))
    page.locator('#podcastAudio').wait_for(state='detached')
    assert not starts, 'reading cached media must not start a generation'
