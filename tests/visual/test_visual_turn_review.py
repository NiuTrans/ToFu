"""Ordinary running Turn → declared outputs → version-bound owner acceptance."""
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.turn_lifecycle.test_task_event_delivery import owner as owner, _task, _discard

pytestmark = [pytest.mark.visual, pytest.mark.auth_mode('open')]


@pytest.mark.parametrize(('width', 'language'), [(1280, 'en'), (390, 'zh')])
def test_ordinary_turn_review_in_actual_application(page, owner, tmp_path, width, language):
    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.artifacts import create_artifact
    from lib.storage import get_storage_client
    from lib.acceptance import turns
    from lib.identity import PrincipalContext
    from tests.conftest import _dismiss_onboarding_modals
    client = get_storage_client(write=True)
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'browser-turn-review')
    artifact = create_artifact(conv_id=task['convId'], task_id=task['id'], content='# Report 1',
        format='markdown', source='write_file', source_ref={'path': 'report.md'}, title='Report 1')
    turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': 'The report is being prepared'})
    labels = json.loads((Path(__file__).resolve().parents[2] / f'frontend/src/i18n/locales/{language}.json').read_text())
    def label(key): return labels[key]
    def button(host, key): return host.get_by_role('button', name=label(key), exact=True)
    principal = PrincipalContext.from_payload(task['_principalContext'])
    try:
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate('lang=>localStorage.setItem("tofu_ui_lang",lang)', language)
        page.add_init_script("window.__reviewAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__reviewAppReady=true})")
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.__reviewAppReady === true'); _dismiss_onboarding_modals(page)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.locator(f'.conv-item[data-conv-id="{task["convId"]}"]').wait_for(state='attached')
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        action = page.locator('[data-conversation-action="review"]')
        action.click()
        dialog = page.locator('[data-turn-review-dialog]')
        review = dialog.locator('[data-turn-review]')
        expect(review).to_contain_text(label('turnReview.executionPending'))
        button(review, 'artifactReview.edit').click()
        review.get_by_label(label('artifactReview.objective'), exact=True).fill('Checked report delivery')
        button(review, 'turnReview.loadOutputs').click()
        review.get_by_label(label('turnReview.outputVersion'), exact=True).select_option(artifact['id'])
        button(review, 'turnReview.includeOutput').click()
        button(review, 'artifactReview.addRequirement').click()
        review.locator('[data-review-draft] input').fill('Answer and report are correct')
        button(review, 'artifactReview.saveRequirements').click()
        expect(review.locator('[data-review-output]')).to_have_count(2)
        item = review.locator('[data-review-criterion]')
        button(item, 'turnReview.selectCurrentOutputs').click()
        item.get_by_label(label('artifactReview.evidence'), exact=True).fill('Compared all outputs to the source')
        button(item, 'artifactReview.confirmPass').click()
        expect(item).to_contain_text(label('artifactReview.pass'))
        expect(review).to_contain_text(label('artifactReview.pending'))
        # A committed answer change must not replace a field being edited or
        # silently turn its selected evidence into a new output version.
        note = item.get_by_label(label('artifactReview.evidence'), exact=True)
        note.fill('Keep this evidence draft'); note.focus()
        handle = note.element_handle()
        note.evaluate('el=>el.setSelectionRange(5, 9)')
        task['content'] = 'Changed while the owner is reviewing'
        turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': task['content']})
        expect(review.locator('[data-review-updates]')).to_contain_text(label('artifactReview.reload'))
        assert handle.evaluate('el=>document.activeElement===el && el.selectionStart===5 && el.selectionEnd===9')
        expect(note).to_have_value('Keep this evidence draft')
        button(review.locator('[data-review-updates]'), 'artifactReview.reload').click()
        expect(item).to_contain_text(label('artifactReview.stale'))
        button(item, 'turnReview.selectCurrentOutputs').click()
        button(item, 'artifactReview.confirmPass').click()
        expect(item).to_contain_text(label('artifactReview.pass'))
        requests = []
        page.on('request', lambda request: requests.append(request.url)
            if '/review' in request.url and request.method == 'GET' else None)
        started = time.monotonic()
        task.update(status='done', finishReason='stop')
        turn_lifecycle.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
        expect(item).to_contain_text(label('artifactReview.stale'))
        elapsed_ms = (time.monotonic() - started) * 1000
        health = page.evaluate("async()=>await (await fetch('/api/health')).json()")
        (tmp_path / f'live-review-{language}-{width}.json').write_text(json.dumps({
            'terminal_commit_to_stale_visible_ms': elapsed_ms,
            'selected_storage_backend': health['storage']['backend'],
            'review_get_requests': len(requests), 'explicit_reload': False,
        }, indent=2))
        button(item, 'turnReview.selectCurrentOutputs').click()
        button(item, 'artifactReview.confirmPass').click()
        expect(review).to_contain_text(label('artifactReview.accepted'))
        # A parallel owner edit changes the generation. No silent draft upgrade.
        current = turns.read_review(task['convId'], task['_turnId'], principal=principal)
        turns.update_review(task['convId'], task['_turnId'], {'action': 'requirements',
            'requirementsRevision': current['requirementsRevision'], 'objective': 'Updated delivery scope',
            'criteria': [{key: row[key] for key in ('id', 'text', 'kind')} for row in current['criteria']],
            'outputs': [{'id': row['id'], 'label': row['label'], 'artifactId': row['artifactId']}
                        for row in current['outputs'] if row['kind'] == 'artifact']}, principal=principal)
        item.get_by_label(label('artifactReview.evidence'), exact=True).fill('Preserve my review note')
        button(item, 'artifactReview.confirmPass').click()
        expect(review.get_by_role('alert').first).to_contain_text(label('artifactReview.saveError'))
        expect(item.get_by_label(label('artifactReview.evidence'), exact=True)).to_have_value('Preserve my review note')
        button(review, 'artifactReview.reload').first.click()
        expect(review).to_contain_text('Updated delivery scope')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
        dialog.evaluate('el=>el.scrollTop=0')
        page.screenshot(path=str(tmp_path / f'turn-review-{language}-{width}.png'), full_page=True)
        button(dialog, 'workingFacts.close').click(); expect(dialog).to_have_count(0)
        action.click(); expect(dialog).to_have_count(1)
        page.evaluate("window.TofuModules.resolveAction('newChat')()")
        expect(dialog).to_have_count(0)
    finally:
        task['aborted'] = True; _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')


@pytest.mark.parametrize(('width', 'language'), [(1280, 'en'), (390, 'zh')])
def test_changed_delivery_keeps_independent_evidence_and_requires_exact_version_review(
        page, owner, tmp_path, width, language):
    from playwright.sync_api import expect
    from lib import turn_lifecycle
    from lib.artifacts import create_artifact
    from lib.storage import get_storage_client
    from tests.conftest import _dismiss_onboarding_modals

    client = get_storage_client(write=True)
    _, task = _task(turn_lifecycle, SimpleNamespace(client=client), 'delivery-correction')
    artifact = create_artifact(conv_id=task['convId'], task_id=task['id'],
        content='# Distance\n10 km', format='markdown', source='write_file', title='Distance v1')
    task.update(content='Distances are expressed in kilometres.', status='done', finishReason='stop')
    turn_lifecycle.record_task_event(task, {'type': 'delta', 'content': task['content']})
    turn_lifecycle.record_task_event(task, {'type': 'done', 'finishReason': 'stop'})
    labels = json.loads((Path(__file__).resolve().parents[2] / f'frontend/src/i18n/locales/{language}.json').read_text())
    def button(host, key): return host.get_by_role('button', name=labels[key], exact=True)
    path = f"/api/v4/conversations/{task['convId']}/turns/{task['_turnId']}/review"
    origin = page.evaluate('location.origin')
    def read():
        response = page.request.get(origin + path)
        assert response.status == 200
        return response.json()['data']
    def open_review():
        page.reload(wait_until='domcontentloaded')
        page.wait_for_function('window.__reviewAppReady === true')
        _dismiss_onboarding_modals(page)
        page.wait_for_function("typeof window.TofuModules?.resolveAction?.('loadConversation')==='function'")
        page.evaluate("id=>window.TofuModules.resolveAction('loadConversation')(id)", task['convId'])
        page.locator('[data-conversation-action="review"]').click()
    try:
        current = read()
        configured = page.request.post(origin + path, data={
            'action': 'requirements', 'requirementsRevision': current['requirementsRevision'],
            'objective': 'Deliver the checked distance report', 'criteria': [
                {'id': 'answer', 'kind': 'requirement', 'text': 'Explain the units'},
                {'id': 'report', 'kind': 'requirement', 'text': 'Show 10 km'},
                {'id': 'units', 'kind': 'constraint', 'text': 'Use kilometres in every output'}],
            'outputs': [{'id': 'report', 'label': 'Distance report', 'artifactId': artifact['id']}]})
        assert configured.status == 200
        page.set_viewport_size({'width': width, 'height': 1000})
        page.evaluate('lang=>localStorage.setItem("tofu_ui_lang",lang)', language)
        page.add_init_script("window.__reviewAppReady=false; window.addEventListener('tofu:app-ready',()=>{window.__reviewAppReady=true})")
        open_review()
        dialog = page.locator('[data-turn-review-dialog]')
        review = dialog.locator('[data-turn-review]')
        def criterion(identifier): return review.locator(f'[data-review-criterion="{identifier}"]')
        def decide(identifier, note, all_outputs=False):
            item = criterion(identifier)
            if all_outputs:
                button(item, 'turnReview.selectCurrentOutputs').click()
            else:
                name = labels['turnReview.answer'] if identifier == 'answer' else 'Distance report'
                item.get_by_role('checkbox', name=name, exact=True).check()
            item.get_by_label(labels['artifactReview.evidence'], exact=True).fill(note)
            button(item, 'artifactReview.confirmPass').click()
            expect(item).to_contain_text(labels['artifactReview.pass'])
        decide('answer', 'The answer explains kilometres.')
        decide('report', 'Inspected the first report: 10 km.')
        decide('units', 'Answer and first report both use kilometres.', all_outputs=True)
        expect(review).to_contain_text(labels['artifactReview.accepted'])
        accepted_v1 = read()

        button(review, 'artifactReview.edit').click()
        review.locator('[data-review-draft="report"] input').fill('Show 12 km')
        button(review, 'artifactReview.saveRequirements').click()
        expect(criterion('report')).to_contain_text(labels['artifactReview.stale'])
        expect(criterion('answer')).to_contain_text(labels['artifactReview.pass'])
        expect(criterion('units')).to_contain_text(labels['artifactReview.pass'])
        criterion('report').get_by_label(labels['artifactReview.evidence'], exact=True).fill('The selected report still says 10 km.')
        button(criterion('report'), 'artifactReview.requestChanges').click()
        expect(review).to_contain_text(labels['artifactReview.changesRequested'])
        requested = read()

        revised = create_artifact(conv_id=task['convId'], task_id=task['id'], parent_id=artifact['id'],
            content='# Distance\n12 km', format='markdown', source='write_file', title='Distance v2')
        button(review, 'artifactReview.edit').click()
        button(review, 'turnReview.loadOutputs').click()
        review.get_by_label(labels['turnReview.outputVersion'], exact=True).select_option(revised['id'])
        review.get_by_label(labels['turnReview.replaceOutput'], exact=True).select_option('report')
        button(review, 'turnReview.replaceVersion').click()
        button(review, 'artifactReview.saveRequirements').click()
        expect(review).to_contain_text(labels['artifactReview.pending'])
        for identifier in ('report', 'units'):
            expect(criterion(identifier)).to_contain_text(labels['artifactReview.stale'])
        expect(criterion('answer')).to_contain_text(labels['artifactReview.pass'])
        expect(review.locator('[data-review-output="report"]')).to_contain_text(labels['turnReview.outputUncovered'])
        pending = read()
        assert pending['criteria'][0]['evidence'] == accepted_v1['criteria'][0]['evidence']
        for host, expected in [(criterion('report').locator('[data-review-output-evidence="report"]'), '10 km'),
                               (review.locator('[data-review-output="report"]'), '12 km')]:
            with page.expect_popup() as popup:
                button(host, 'turnReview.openOutput').click()
            preview = popup.value
            expect(preview.locator('body')).to_contain_text(expected)
            assert preview.evaluate('window.opener === null')
            preview.close()
        dialog.evaluate('el=>el.scrollTop=0')
        page.screenshot(path=str(tmp_path / f'changed-delivery-pending-{language}-{width}.png'), full_page=True, animations='disabled')
        # Selecting a new version is explicit; retained historical evidence does
        # not authorize the new artifact or a global constraint over that artifact.
        decide('report', 'Opened the selected new report and checked 12 km.')
        expect(review).to_contain_text(labels['artifactReview.pending'])
        decide('units', 'Rechecked the answer and new report: both use kilometres.', all_outputs=True)
        expect(review).to_contain_text(labels['artifactReview.accepted'])
        accepted_v2 = read()
        assert accepted_v2['outputs'][1]['artifactId'] == revised['id']
        assert accepted_v2['outputs'][1]['version'] != accepted_v1['outputs'][1]['version']
        open_review()
        expect(review).to_contain_text(labels['artifactReview.accepted'])
        assert read() == accepted_v2
        expect(dialog).to_have_css('opacity', '1')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
        assert dialog.evaluate('el=>el.scrollWidth<=el.clientWidth+1')
        page.screenshot(path=str(tmp_path / f'changed-delivery-accepted-{language}-{width}.png'), full_page=True, animations='disabled')
        (tmp_path / 'changed-delivery-evidence.json').write_text(json.dumps({
            'accepted_v1': accepted_v1, 'changes_requested': requested,
            'pending_v2': pending, 'accepted_v2_after_reload': accepted_v2,
        }, indent=2))
    finally:
        task['aborted'] = True; _discard(task)
        client.command('conversation.delete', {'conv_id': task['convId'], 'user_id': 1}, task['id'] + '-cleanup')
