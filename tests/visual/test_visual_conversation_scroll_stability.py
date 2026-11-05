"""Reading through long history never hits the mounted edge or jumps.

Real app, shipped styles and renderers, remote history pages. The Surface
mounts history geometrically ahead of the reader, so the browser's native
scroll anchoring absorbs every insertion: no programmatic correction from the
mounted edge (which would cancel smooth/momentum scrolling and clamp the
gesture), no movement against the gesture and no visible block vanishing.
"""
import json
import os
import uuid
from pathlib import Path

import pytest

from tests.support.conversation_scroll_stability import (
    SAMPLER, dump, seed_transcript, summarize_frames,
)

pytestmark = pytest.mark.visual

AT_FIRST_TURN = """() => {const v=document.querySelector('#chatContainer');
  const s=document.querySelector('#chatInner .conversation-surface');
  return v.scrollTop<=0.5 && s?.dataset.windowStart==='0'
    && !document.querySelector('#chatInner [data-history-state="loading"]')
    && (document.querySelector('#chatInner [data-turn-id]')?.textContent||'').includes('Question 0.');}"""
AT_LATEST = """() => {const v=document.querySelector('#chatContainer');
  const s=document.querySelector('#chatInner .conversation-surface');
  return v.scrollHeight-v.scrollTop-v.clientHeight<=2 && s?.dataset.hasLaterContent==='false';}"""


def _wheel(page, delta, events, done):
    for _ in range(events):
        page.mouse.wheel(0, delta)
        page.wait_for_timeout(16)
        if page.evaluate(done):
            break
    page.wait_for_timeout(400)


@pytest.mark.parametrize('profile,width', [('mixed', 1180), ('tiny', 390)])
def test_history_reading_stays_anchored_without_edge_corrections(
        browser, live_server, tmp_path, profile, width):
    from playwright.sync_api import expect
    from tests.conftest import _dismiss_onboarding_modals

    out = Path(os.environ.get('SCROLL_STABILITY_OUT') or tmp_path)
    out.mkdir(parents=True, exist_ok=True)
    rounds = {'mixed': 80, 'tiny': 150}[profile]
    cid = 'scroll-stability-' + uuid.uuid4().hex[:10]
    context = browser.new_context(viewport={'width': width, 'height': 800}, service_workers='block')
    context.add_init_script(f"sessionStorage.setItem('tofu_activeConvId', '{cid}');")
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        seed_transcript(context, live_server, cid, rounds=rounds, profile=profile)
        page.goto(live_server, wait_until='domcontentloaded')
        _dismiss_onboarding_modals(page)
        surface = page.locator(f'#chatInner .conversation-surface[data-conversation-id="{cid}"]')
        expect(surface).to_contain_text(f'Answer {rounds - 1}.', timeout=20000)
        page.wait_for_timeout(1000)
        _dismiss_onboarding_modals(page)
        expect(page.locator('.modal-overlay.open')).to_have_count(0)
        page.evaluate(SAMPLER)
        page.locator('#chatContainer').hover()
        # Wheel steps read the whole transcript; fling steps outrun one
        # viewport per frame, the hardest case for mounting ahead of the reader.
        phases = [
            ('up-wheel', 'up', -300, 1500, AT_FIRST_TURN),
            ('down-wheel', 'down', 300, 1500, AT_LATEST),
            ('up-fling', 'up', -900, 160, AT_FIRST_TURN),
        ]
        reports = {}
        for name, direction, delta, events, done in phases:
            page.evaluate('window.__scrollProbe.start()')
            _wheel(page, delta, events, done)
            data = page.evaluate('window.__scrollProbe.stop()')
            report = summarize_frames(data, direction, abs(delta))
            reports[name] = report
            dump(out / f'{profile}-{width}-{name}.json', report)
            # Each gesture reads continuously from the latest Turn to the first
            # one, through every remote history page, and back.
            if name != 'up-fling':
                assert page.evaluate(done), (name, report)
            assert report['edge_writes'] == [], (name, report)
            assert report['reverse'] == [], (name, report)
            assert report['uneven'] == [], (name, report)
            assert report['vanished'] == [], (name, report)
            assert 0 < report['max_turns'] <= 80, (name, report)
        print(f'Scroll stability evidence ({profile}, {width}px): '
              + json.dumps({name: {key: value for key, value in report.items()
                                   if not isinstance(value, list)}
                            for name, report in reports.items()}))
        assert not errors, errors
    finally:
        context.request.delete(f'{live_server}/api/v4/conversations/{cid}')
        context.close()
