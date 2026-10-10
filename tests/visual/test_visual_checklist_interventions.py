"""Checklist recovery notes: real composition/segments and served Chromium render.

Synthetic task state never enters a conversation/store. This is a settled
renderer/disclosure test, not an agent-loop, streaming, or persistence journey.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

import pytest

from lib.tasks_pkg.context_composer.checklist import restore_checklist_context
from lib.tasks_pkg.segments import assemble_segments
from lib.tools.todo import apply_todo_write_to_task

pytestmark = pytest.mark.visual


_HOSTILE_EXPLANATION = (
    'Progress only, not a success verdict. '
    '<img src="missing-checklist-image" onerror="window.checklistXss=true"> '
    '<script>window.checklistXss=true</script> & "quoted"'
)


@pytest.fixture
def checklist_visual_assets(flask_app, monkeypatch):
    """Optionally read an existing bundle; never build/copy/edit shipped assets.

    TOFU_VISUAL_STATIC_ROOT is explicit because an isolated worktree may lack
    ignored Vite output. The supplied directory is read-only and only replaces
    static-file resolution in this pytest process, not the guarded storage.
    """
    import server
    from lib import vite_assets
    from lib.static_serving import load_static_bytes

    static_root = Path(os.environ.get('TOFU_VISUAL_STATIC_ROOT') or
                       Path(__file__).resolve().parents[2] / 'static').resolve()
    manifest_path = static_root / 'vite' / 'manifest.json'
    assert manifest_path.is_file(), (
        'Prebuilt Vite manifest required; set TOFU_VISUAL_STATIC_ROOT to an '
        'existing static directory for a build-free run.')
    monkeypatch.setattr(vite_assets, 'VITE_OUT_DIR', str(static_root / 'vite'))
    monkeypatch.setattr(vite_assets, 'VITE_MANIFEST', str(manifest_path))
    monkeypatch.setattr(server, '_load_static_bytes',
                        lambda name: load_static_bytes(str(static_root), name))
    vite_assets.clear_vite_asset_cache()
    manifest = vite_assets.validate_published_vite_artifact()
    entry = manifest['frontend/src/main.ts']['file']
    yield {'staticRoot': str(static_root), 'mainEntry': entry,
           'manifestSha256': hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
           'mainSha256': hashlib.sha256((static_root / 'vite' / entry).read_bytes()).hexdigest()}
    vite_assets.clear_vite_asset_cache()


def _tool(round_num, index, narration=''):
    return {
        'toolName': 'read_files', 'toolCallId': f'checklist-{round_num}-{index}',
        'roundNum': index, 'llmRound': round_num, 'status': 'done',
        'assistantContent': narration,
        'toolArgs': {'path': f'synthetic/round-{round_num}-{index}.txt'},
        'results': [{'text': 'Synthetic read result; no filesystem tool ran.'}],
    }


def _composed_timeline():
    task = {'toolRounds': [
        _tool(5, 1, 'Earlier model round 5, before recovery.'), _tool(5, 2),
    ]}
    checkpoints = []
    for round_num, explanation, status in (
        (8, 'First pass recorded; validation is still pending.', 'in_progress'),
        (9, _HOSTILE_EXPLANATION, 'completed'),
    ):
        task['_activeRoundNum'] = round_num
        checkpoints.append({'activeRound': round_num, 'toolCount': len(task['toolRounds'])})
        assert round_num != len(task['toolRounds'])
        _, _, outcome = apply_todo_write_to_task(task, {
            'todos': [
                {'content': 'Inspect accepted work', 'status': 'completed'},
                {'content': 'Validate the changed behavior', 'status': status},
            ],
            'explanation': explanation,
        })
        assert not outcome['rejected']
        accepted = copy.deepcopy(task['_todoState'])
        # Simulate lost checklist evidence, not compaction itself. The actual
        # composer records the resulting note at the active consuming round.
        messages = [{'role': 'user', 'content': 'Continue the synthetic task.'}]
        restore_checklist_context(messages, task=task)
        assert task['_todoState'] == accepted  # restoration is not an update
        assert len(messages) == 2
        content = messages[-1]['content']
        wire_text = (content if isinstance(content, str) else
                     '\n'.join(block['text'] for block in content if block['type'] == 'text'))
        assert explanation in wire_text
        assert 'chatui-todo' not in wire_text
        task['toolRounds'].extend([
            _tool(round_num, len(task['toolRounds']) + 1,
                  f'Consuming model round {round_num}, after checklist recovery.'),
            _tool(round_num, len(task['toolRounds']) + 2),
        ])

    segments = assemble_segments(task)
    notes = [s for s in segments if s['type'] == 'system_note']
    assert [(n['reason'], n['llmRound']) for n in notes] == [
        ('active_checklist', 8), ('active_checklist', 9),
    ]
    for note in notes:
        index = segments.index(note)
        assert segments[index - 1]['type'] == 'tool_use'
        assert segments[index - 1]['llmRound'] < note['llmRound']
        assert segments[index + 1]['type'] == 'text'
        assert segments[index + 1]['llmRound'] == note['llmRound']
        assert 'Progress explanation from the accepted todo update:' in note['text']
        assert 'Restoration is not an update.' in note['text']
        assert 'Status reports progress, not proof of success.' in note['text']
        assert 'chatui-todo' not in note['text']
    return {'role': 'assistant', 'segments': segments,
            'toolRounds': task['toolRounds']}, notes, checkpoints


def test_checklist_interventions_precede_consuming_rounds_and_escape_explanation(
        browser, live_server, checklist_visual_assets, tmp_path):
    message, notes, checkpoints = _composed_timeline()
    errors = []
    evidence = {'compositionCheckpoints': checkpoints, 'viewports': [],
                'assets': checklist_visual_assets}
    with browser.new_context(locale='en-US') as context:
        context.add_init_script("localStorage.setItem('tofu_ui_lang', 'en')")
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        response = page.goto(live_server, wait_until='networkidle')
        assert response is not None and response.status == 200
        page.wait_for_function("typeof window.renderSegmentTimelineHTML === 'function'")
        # Use the shipped renderer + its real batching/i18n/escaping dependencies.
        # No runtime-section compilation, source extraction, or render stubs.
        markup = page.evaluate(
            'msg => window.renderSegmentTimelineHTML(msg.segments, msg, 0)', message)
        assert markup
        page.evaluate("""() => {
          const frame = document.createElement('iframe');
          frame.id = 'checklist-interventions-fixture';
          frame.style.cssText = 'position:fixed;inset:16px;width:calc(100% - 32px);'
            + 'max-width:1024px;height:calc(100% - 32px);z-index:99999;background:white';
          document.body.append(frame);
        }""")
        frame = page.frames[-1]
        frame.set_content('<!doctype html><html lang="en" data-theme="light">'
                          '<body><main id="timeline" class="conversation-surface">'
                          '</main></body></html>')
        frame.add_style_tag(url=f'{live_server}/static/styles.css')
        # Only the fixture shell is styled here; note/timeline layout uses shipped CSS.
        frame.add_style_tag(content='body{display:block;margin:0;padding:12px;'
                                   'height:auto;overflow:auto}#timeline{width:100%}')
        frame.evaluate("""html => {
          window.checklistXss = false;
          document.querySelector('#timeline').innerHTML = html;
          // The outer tool-panel click delegate belongs to the app document.
          // Expand just that shell; note disclosures below use native clicks.
          document.querySelector('.ptool-panel').classList.remove('collapsed');
        }""", markup)
        rows = frame.locator('.sw-system-note-row')
        assert rows.count() == 2
        assert frame.locator('.sw-note-group').count() == 0
        assert frame.locator('.ptool-turn').count() == 3
        assert frame.locator('.ptool-panel-body').get_attribute('data-full-count') == '6'

        expected_order = ['round:5', 'tools:L5', 'note:8', 'round:8',
                          'tools:L8', 'note:9', 'round:9', 'tools:L9']
        # A tall narrow viewport keeps both expanded notes in the screenshot;
        # this proves width/wrapping, not phone-height scrolling ergonomics.
        for label, width, height in (('desktop', 1280, 1000), ('narrow', 390, 1800)):
            page.set_viewport_size({'width': width, 'height': height})
            frame.evaluate("""() => document.querySelectorAll('.sw-system-note-row')
              .forEach(row => row.open = false)""")
            for i, note in enumerate(notes):
                row = rows.nth(i)
                assert row.get_attribute('data-reason') == 'active_checklist'
                assert row.locator('summary .ptool-text').inner_text() == (
                    'System intervention · Active checklist')
                assert row.locator('summary .ptool-badge').inner_text() == 'Context appended'
                assert not row.locator('pre').is_visible()
                row.locator('summary').click()
                assert row.locator('pre').is_visible()
                assert row.locator('pre').inner_text() == note['text']
                assert row.locator('pre > *').count() == 0
            assert _HOSTILE_EXPLANATION in rows.nth(1).locator('pre').inner_text()
            assert frame.locator('#timeline script, #timeline img, #timeline [onerror]').count() == 0
            assert frame.evaluate('window.checklistXss') is False
            assert 'chatui-todo' not in frame.locator('#timeline').inner_text()

            dom = frame.evaluate("""() => {
              const nodes = [...document.querySelector('.ptool-panel-body').children];
              let noteIndex = 0;
              return {
                order: nodes.map(node => {
                  if (node.matches('.sw-system-note-row')) return 'note:' + [8,9][noteIndex++];
                  if (node.matches('.seg-narration')) return 'round:' + node.dataset.segRound.slice(1);
                  if (node.matches('.ptool-turn')) return 'tools:' + node.dataset.llmRound;
                  return 'unexpected:' + node.className;
                }),
                notes: [...document.querySelectorAll('.sw-system-note-row')].map(row => ({
                  label: row.querySelector('summary .ptool-text').textContent,
                  text: row.querySelector('pre').textContent,
                  open: row.open,
                  preChildren: row.querySelector('pre').children.length,
                  width: row.getBoundingClientRect().width,
                  right: row.getBoundingClientRect().right,
                })),
                clientWidth: document.documentElement.clientWidth,
                scrollWidth: document.documentElement.scrollWidth,
                timelineBottom: document.querySelector('#timeline').getBoundingClientRect().bottom,
                frameHeight: window.innerHeight,
                xssExecuted: window.checklistXss,
              };
            }""")
            assert dom['order'] == expected_order
            assert dom['scrollWidth'] <= dom['clientWidth'] + 1

            assert dom['timelineBottom'] <= dom['frameHeight']
            assert all(0 < n['width'] and n['right'] <= dom['clientWidth'] + 1
                       for n in dom['notes'])
            screenshot = tmp_path / f'checklist-interventions-{label}.png'
            # The containment assertion above prevents iframe clipping or
            # capturing the underlying app in a tall locator screenshot.
            frame.locator('#timeline').screenshot(path=str(screenshot))
            evidence['viewports'].append({'name': label, 'width': width, 'height': height,
                                          'screenshot': str(screenshot), 'dom': dom})
            print(f'Checklist intervention screenshot ({label}): {screenshot}')
        assert not errors, errors
        evidence['pageErrors'] = errors
        artifact = tmp_path / 'checklist-interventions-dom.json'
        artifact.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n')
        print(f'Checklist intervention DOM evidence: {artifact}')
