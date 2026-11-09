"""Content-free investigations join actual request bytes to registered sources."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from lib.tasks_pkg.context_composer import (
    ComposeRequest, append_injection, context_block, render_context,
)
from lib.tasks_pkg.context_composer.inspection import compare_context, inspect_context

pytestmark = pytest.mark.unit


def snapshot(*blocks):
    messages = [{'role': 'user', 'content': 'private human request'}]
    result = render_context(messages, list(blocks), ComposeRequest())
    return {'messages': messages, 'contextManifest': result.manifest}


def test_inspection_reads_actual_amalgamated_sections_and_registry_without_bodies():
    document = snapshot(context_block('user_context', 'private preference'),
                        context_block('memory_guidance', 'private memory'))
    original = deepcopy(document)
    report = inspect_context(document)
    origins = [o for m in report['messages'] for o in m['origins']]
    assert {o['id'] for o in origins} == {'user_context', 'memory_guidance'}
    for origin in origins:
        assert origin['evidence'] == 'managed_carrier'
        assert origin['source'] and origin['matchesReceipt']
        assert origin['decisions'][0]['appended']
    assert 'private' not in json.dumps(report)
    assert document == original


def test_in_place_mutation_ignores_stale_metadata_and_names_only_changed_source():
    before = snapshot(context_block('user_context', 'private preference'),
                      context_block('memory_guidance', 'private memory'))
    after = deepcopy(before)
    for part in after['messages'][-1]['content']:
        part['text'] = part['text'].replace('private preference', 'changed preference')
    report = compare_context(before, after)
    assert report['messageChange'] == 'prefix_changed'
    first = report['firstMessageDifference']
    assert first['index'] == len(before['messages']) - 1
    assert first['fields'] == ['content']
    assert [o['id'] for o in first['changedOrigins']] == ['user_context']
    assert not first['afterOrigins'][0]['matchesReceipt'] or not first['afterOrigins'][-1]['matchesReceipt']
    assert report['cacheVerdict'] == 'not_evaluated'
    assert 'private' not in json.dumps(report)


def test_superseding_snapshot_is_append_only_and_receipts_are_not_wire_evidence():
    before = snapshot(context_block('user_context', 'A'))
    after = deepcopy(before)
    result = render_context(after['messages'], [context_block('user_context', 'B')], ComposeRequest())
    after['contextManifest'] += result.manifest
    report = compare_context(before, after)
    assert report['messageChange'] == 'append_only'
    assert report['commonPrefixMessages'] == len(before['messages'])
    assert report['firstMessageDifference'] is None
    after['contextManifest'][0]['reason'] = 'different receipt only'
    assert compare_context(after, deepcopy(after))['messageChange'] == 'unchanged'


def test_withdrawing_policy_does_not_blame_unchanged_carriers_that_shift_position():
    before = snapshot(context_block('platform_static', 'Contract'),
                      context_block('ui_nav_directory', 'Directory'),
                      context_block('user_context', 'Preference'))
    after = deepcopy(before)
    render_context(after['messages'], [context_block('platform_static', '', reason='replace_mode')],
                   ComposeRequest())
    first = compare_context(before, after)['firstMessageDifference']
    assert first['index'] == 0
    assert [origin['id'] for origin in first['changedOrigins']] == ['platform_static']


def test_event_occurrences_keep_identity_but_verify_actual_text():
    messages = [{'role': 'user', 'content': 'request'}]
    append_injection(messages, 'tool_progress', 'private instruction')
    first = inspect_context({'messages': messages})['messages'][-1]['origins'][0]
    assert first['evidence'] == 'event_receipt' and first['matchesReceipt']
    messages[-1]['content'] = 'tampered instruction'
    second = inspect_context({'messages': messages})['messages'][-1]['origins'][0]
    assert second['eventId'] == first['eventId']
    assert not second['matchesReceipt'] and second['hash'] != first['hash']


def test_legacy_exported_snapshot_recovers_event_candidates_without_guessing_occurrence():
    from lib.tasks_pkg.round_evidence import build_round_evidence
    messages = [{'role': 'user', 'content': 'request'}]
    task = {'id': 'task', '_attemptId': 'attempt'}
    for _ in range(2):
        messages.append({'role': 'assistant', 'content': 'Observed result'})
        append_injection(messages, 'tool_progress', 'private instruction', task=task)
    event = build_round_evidence(task, messages, ref={'roundNum': 1, 'turn': 'turn'},
                                 kind='request', model='model')
    assert '_contextInjection' not in event['messages'][-1]
    event.pop('contextLineage', None)  # Archive written before transformation witnesses.
    origins = inspect_context(event)['messages'][-1]['origins']
    assert len(origins) == 2
    assert all(origin['evidence'] == 'receipt_hash_match' for origin in origins)
    assert len({origin['eventId'] for origin in origins}) == 2
    assert 'private instruction' not in json.dumps(inspect_context(event))


def test_legacy_merged_event_without_exact_content_witness_stays_explicitly_unlocated():
    from lib.tasks_pkg.round_evidence import build_round_evidence
    messages = [{'role': 'user', 'content': 'request'}]
    task = {'id': 'task', '_attemptId': 'attempt'}
    append_injection(messages, 'tool_progress', 'private instruction', task=task)
    event = build_round_evidence(task, messages, ref={'roundNum': 1, 'turn': 'turn'},
                                 kind='request', model='model')
    event.pop('contextLineage', None)  # Historical missing evidence cannot be reconstructed.
    report = inspect_context(event)
    assert not any(message['origins'] for message in report['messages'])
    receipt, = report['unlocatedEventReceipts']
    assert receipt['id'] == 'tool_progress'
    assert receipt['reason'] == 'no_exact_carrier_match'
    assert 'private instruction' not in json.dumps(report)


def test_marker_ttl_change_is_visible_even_when_prefix_content_is_unchanged():
    before = {'messages': [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'same text', 'cache_control': {'type': 'ephemeral'}}]}]}
    after = deepcopy(before)
    after['messages'][0]['content'][0]['cache_control']['ttl'] = '1h'
    report = compare_context(before, after, wire=True)
    assert report['messageChange'] == 'unchanged'
    assert report['cacheMarkersChanged'] and report['cacheVerdict'] == 'not_evaluated'


def test_hoisted_bootstrap_receipt_is_located_without_message_metadata():
    messages, task = [], {}
    append_injection(messages, 'worker_policy', 'Worker policy', task=task)
    document = {'system': 'Worker policy', 'messages': [],
                'contextManifest': task['_contextManifest']}
    report = inspect_context(document, wire=True)
    assert not report['unlocatedEventReceipts']
    assert report['systemOrigins'][0]['id'] == 'worker_policy'
    assert report['systemOrigins'][0]['evidence'] == 'receipt_hash_match'


def test_quoted_markers_and_wire_markers_are_observations_not_proof_of_ownership():
    document = snapshot(context_block('platform_static', 'Policy'))
    carrier = document['messages'][0]
    quote = {'role': 'user', 'content': deepcopy(carrier['content'])}
    origin = inspect_context({'messages': [quote]})['messages'][0]['origins'][0]
    assert origin['evidence'] == 'marker_only'
    assert origin['matchesReceipt'] is None
    wire = {'system': carrier['content'], 'messages': [quote]}
    report = inspect_context(wire, wire=True)
    assert report['systemOrigins'][0]['id'] == 'platform_static'
    assert report['systemOrigins'][0]['evidence'] == 'marker_only'


def test_hoisted_policy_and_tool_changes_are_separate_from_message_prefix():
    before = snapshot(context_block('platform_static', 'Policy'))
    before = {'system': before['messages'][0]['content'],
              'messages': [{'role': 'user', 'content': 'request'}],
              'tools': [{'name': 'read', 'description': 'original'}]}
    after = deepcopy(before)
    after['system'][0]['text'] = after['system'][0]['text'].replace('Policy', 'Changed')
    after['tools'][0]['description'] = 'changed'
    report = compare_context(before, after, wire=True)
    assert report['messageChange'] == 'unchanged'
    assert report['changedRegions'] == ['system', 'tools']
    assert [o['id'] for o in report['systemChangedOrigins']] == ['platform_static']


def test_shortening_and_shape_changes_do_not_disappear_as_unchanged():
    before = {'messages': [{'role': 'user', 'content': 'A'}, {'role': 'assistant', 'content': 'B'}]}
    after = {'messages': before['messages'][:1]}
    report = compare_context(before, after)
    assert report['messageChange'] == 'history_shortened'
    assert report['firstMessageDifference']['index'] == 1
    wrapped = {'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'A'}]}]}
    report = compare_context(after, wrapped, wire=True)
    assert report['messageChange'] == 'prefix_changed'
    assert report['firstMessageDifference']['semanticChange'] is False


def test_responses_input_and_instructions_are_inspected():
    before = {'model': 'example', 'instructions': 'private policy',
              'input': [{'type': 'message', 'role': 'user', 'content': 'A'}]}
    after = deepcopy(before)
    after['instructions'] = 'changed policy'
    report = compare_context(before, after, wire=True)
    assert report['changedRegions'] == ['system']
    assert report['messageChange'] == 'unchanged'
    assert 'private' not in json.dumps(report)


@pytest.mark.parametrize('invalid', [{}, {'messages': None}, {'messages': [1]},
                                   {'messages': [], 'input': []}])
def test_incomplete_or_ambiguous_documents_fail_closed(invalid):
    with pytest.raises(ValueError):
        inspect_context(invalid)


def test_cli_reads_exported_files_and_rejects_invalid_input(tmp_path):
    root = Path(__file__).resolve().parents[2]
    old = tmp_path / 'before.json'
    new = tmp_path / 'after.json'
    old.write_text(json.dumps(snapshot(context_block('user_context', 'secret-one'))))
    new.write_text(json.dumps(snapshot(context_block('user_context', 'secret-two'))))
    command = [sys.executable, str(root / 'scripts/inspect_context.py'), str(old), str(new)]
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert report['firstMessageDifference']['changedOrigins'][0]['id'] == 'user_context'
    assert 'secret-' not in result.stdout
    new.write_text('{ invalid secret-input')
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 2 and not result.stdout
    assert 'secret-input' not in result.stderr
