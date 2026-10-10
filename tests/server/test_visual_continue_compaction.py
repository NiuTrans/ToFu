"""Real app browser renders compression progress and unarchived failures."""
import pytest

from tests._runtime_sections import ROOT, native_module_path

pytestmark = pytest.mark.visual


@pytest.mark.parametrize('width', [1100, 390])
def test_compaction_progress_and_archive_failure_are_visible(page, width):
    from playwright.sync_api import expect
    page.set_viewport_size({'width': width, 'height': 800})
    page.add_script_tag(path=native_module_path('.native/continue-activity-anchor.js',
        ROOT / 'frontend/src/conversation/presentation/activity-anchor.ts'))
    page.add_script_tag(path=native_module_path('.native/continue-activity-renderer.js',
        ROOT / 'frontend/src/conversation/ui/renderers/activity-block.ts'))
    from lib.agent_core.events import build_phase, Phase
    from lib.turn_activity_timeline import fold_activity_timeline
    from lib.tasks_pkg.compaction._archive import fail_compaction_archive
    import lib.tasks_pkg.manager
    from unittest.mock import patch
    from lib.storage.errors import StorageError
    task = {'id':'task', '_attemptId':'attempt', 'convId':'conversation'}
    start = build_phase(Phase.COMPACTING, detail='Compressing earlier context to fit the window…')
    start.update(seq=1, emittedAt=1000)
    timeline = fold_activity_timeline(None, start, task)
    render = """entries => {
      let host = document.querySelector('#compaction-regression');
      if (!host) {host = document.createElement('main'); host.id = 'compaction-regression';
        document.body.append(host);}
      host.replaceChildren();
      for (const entry of entries) {
        if (!activityEntryIsVisible(entry)) continue;
        const node = document.createElement('div'); host.append(node);
        renderActivityEventBlock(node, {kind:'activity-event', value:entry}, {});
      }
    }"""
    page.evaluate(render, timeline['entries'])
    host = page.locator('#compaction-regression')
    expect(host.get_by_text('Compressing earlier context to fit the window…')).to_be_visible()
    events = []
    with patch.object(lib.tasks_pkg.manager, 'append_event', lambda task, event: events.append(event)):
        with pytest.raises(StorageError):
            fail_compaction_archive(task, None, {})
    events[0].update(seq=2, emittedAt=2000)
    timeline = fold_activity_timeline(timeline, events[0], task)
    page.evaluate(render, timeline['entries'])
    expect(host.get_by_text('Could not save compacted context; original context retained.')).to_be_visible()
    expect(host.get_by_text('Compressing earlier context to fit the window…')).to_have_count(0)
    expect(host.locator('[data-conversation-action="open-compaction"]')).to_have_count(0)
    expect(host.locator('.activity-event')).to_have_count(1)
