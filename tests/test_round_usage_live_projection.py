"""v2 live context-gauge feed — 2026-08-23 "context sphere frozen during
generation" root-fix pins.

Root cause: under turns-protocol v2 the frontend's v1 SSE lane (whose
``round_usage`` handler refreshed the context-health bar per LLM round)
never runs, and the v2 projection only gained ``apiRounds`` at finalize —
so mid-turn the durable turn projection carried NO per-round prompt size
and nothing repainted the gauge.

Fix chain pinned here:
  1. ``llm_fallback._emit_round_usage`` stashes a COMPACT
     ``task['_lastRoundUsage']`` (usage plus a credential-free resolved route)
     before append_event — never the raw usage dict (``_wire_*`` diagnostics
     are GiB-class bloat on durable rows, measured 2026-08-20).
  2. ``turn_lifecycle._task_projection`` folds it into the durable turn
     projection as ``lastRoundUsage`` (both sidecar and legacy branches
     share this fold point; slim frames patch only content/thinking, so the
     reading survives delta windows and reconnect tail-hydration).
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


def _task():
    return {'id': 'task-livegauge1', 'convId': 'conv-1', 'config': {}}


def test_emit_stashes_compact_last_round_usage_anthropic(monkeypatch):
    """Anthropic convention: prompt_tokens excludes cache → tokensIn is
    inp + cache_write + cache_read when the residual fits inside cache."""
    import lib.tasks_pkg.llm_fallback._usage as usage_mod

    sent = []
    monkeypatch.setattr(usage_mod, 'append_event',
                        lambda task, ev: sent.append(ev))
    task = _task()
    raw_usage = {
        'input_tokens': 2000,
        'cache_creation_input_tokens': 40000,
        'cache_read_input_tokens': 120000,
        'output_tokens': 500,
        '_wire_fp': 'x' * 100,
        '_wire_bytes': ['large-history-evidence'],
        '_wire_static': 'static-prefix',
    }
    usage_mod._emit_round_usage(
        task, 3, 'claude-opus-5', raw_usage, tag='R3')

    stash = task.get('_lastRoundUsage')
    assert stash == {'round': 3, 'model': 'claude-opus-5', 'tag': 'R3',
                     'tokensIn': 162000, 'tokensOut': 500}
    # The live event carries public usage and the compact experiment join key,
    # never full-history backend evidence. The same-round raw owner is intact.
    assert sent and sent[0]['type'] == 'round_usage'
    assert sent[0]['tokensIn'] == 162000
    assert sent[0]['tokensOut'] == 500
    assert sent[0]['usage']['_wire_static'] == 'static-prefix'
    assert '_wire_fp' not in sent[0]['usage']
    assert '_wire_bytes' not in sent[0]['usage']
    assert raw_usage['_wire_fp'] == 'x' * 100


def test_round_record_projection_has_a_fixed_evidence_budget():
    """Consumed history evidence cannot grow the retained round record."""
    from lib.tasks_pkg.llm_fallback._usage import (
        project_usage_for_round_record,
    )

    raw_usage = {
        'prompt_tokens': 120_000,
        'completion_tokens': 800,
        'trace_id': 'trace-keep',
        '_dispatch': {'provider_id': 'provider-keep'},
        '_wire_static': 'a' * 16,
        '_wire_fp': [{'fields': {'content': 'x' * 128}}] * 500,
        '_wire_bytes': [{'key': 'k', 'h': 'y' * 32}] * 500,
        '_wire_field_bytes': [
            {'key': 'k', 'fields': {'content': 'z' * 32}}
        ] * 500,
        '_wire_markers': {'msg': list(range(500))},
        '_extra_billing_rounds': [{
            'usage': {'prompt_tokens': 119_000, '_wire_fp': ['nested'] * 500},
        }],
    }

    projected = project_usage_for_round_record(raw_usage)

    assert projected == {
        'prompt_tokens': 120_000,
        'completion_tokens': 800,
        'trace_id': 'trace-keep',
        '_dispatch': {'provider_id': 'provider-keep'},
        '_wire_static': 'a' * 16,
    }
    assert '_wire_fp' in raw_usage
    raw_size = len(json.dumps(raw_usage, separators=(',', ':')))
    projected_size = len(json.dumps(projected, separators=(',', ':')))
    assert projected_size < raw_size * 0.01


def test_emit_stashes_openai_convention_prompt_includes_cache(monkeypatch):
    """OpenAI convention: prompt_tokens already includes cache → tokensIn is
    the raw input number, never double-counted."""
    import lib.tasks_pkg.llm_fallback._usage as usage_mod

    monkeypatch.setattr(usage_mod, 'append_event', lambda task, ev: None)
    task = _task()
    usage_mod._emit_round_usage(task, 1, 'gpt-5.3', {
        'prompt_tokens': 88000,
        'completion_tokens': 1200,
    }, tag='R1')
    assert task['_lastRoundUsage']['tokensIn'] == 88000
    assert task['_lastRoundUsage']['tokensOut'] == 1200


def test_emit_stashes_credential_free_response_route(monkeypatch):
    """The finish footer gets an explicit serving route without retaining the
    API key or unrelated dispatcher timing diagnostics."""
    import lib.tasks_pkg.llm_fallback._usage as usage_mod

    monkeypatch.setattr(usage_mod, 'append_event', lambda task, ev: None)
    task = _task()
    usage_mod._emit_round_usage(task, 2, 'sol', {
        'prompt_tokens': 1200,
        'completion_tokens': 50,
        '_dispatch': {
            'model': 'gpt-5.6-sol',
            'provider_id': 'oauth_codex',
            'key': 'codex_slot_0',
            'key_tail': '9abc',
            'api_key': 'must-not-project',
            'latency_ms': 1234,
        },
    }, tag='R2')

    assert task['_lastRoundUsage'] == {
        'round': 2,
        'model': 'sol',
        'resolvedModel': 'gpt-5.6-sol',
        'providerId': 'oauth_codex',
        'keyName': 'codex_slot_0',
        'keyTail': '9abc',
        'tag': 'R2',
        'tokensIn': 1200,
        'tokensOut': 50,
    }


def test_billed_discarded_round_emits_but_does_not_replace_response(monkeypatch):
    import lib.tasks_pkg.llm_fallback._usage as usage_mod

    sent = []
    monkeypatch.setattr(usage_mod, 'append_event',
                        lambda task, event: sent.append(event))
    task = _task()
    usage_mod._emit_round_usage(task, 1, 'kimi-k3', {
        'prompt_tokens': 30000,
        'completion_tokens': 100,
    }, tag='R1')
    authoring = dict(task['_lastRoundUsage'])

    usage_mod._emit_round_usage(task, 1, 'gemini-discarded', {
        'prompt_tokens': 31000,
        'completion_tokens': 1,
    }, tag='R1-DISCARDED', response_authoring=False)

    assert task['_lastRoundUsage'] == authoring
    assert len(sent) == 2
    assert sent[-1]['model'] == 'gemini-discarded'


def test_emit_no_usage_no_stash(monkeypatch):
    import lib.tasks_pkg.llm_fallback._usage as usage_mod

    monkeypatch.setattr(usage_mod, 'append_event', lambda task, ev: None)
    task = _task()
    usage_mod._emit_round_usage(task, 1, 'm', None, tag='R1')
    assert '_lastRoundUsage' not in task


def test_task_projection_folds_last_round_usage():
    """The projection fold exposes the stash under the camelCase contract
    the frontend reads (``msg.lastRoundUsage.tokensIn``)."""
    from lib.turn_lifecycle import _task_projection

    task = _task()
    task['_lastRoundUsage'] = {'round': 2, 'model': 'm2', 'tag': 'R2',
                               'tokensIn': 47000, 'tokensOut': 300}
    projection = _task_projection(task, {})
    assert projection['lastRoundUsage'] == {
        'round': 2, 'model': 'm2', 'tag': 'R2',
        'tokensIn': 47000, 'tokensOut': 300}


def test_task_projection_carries_reading_across_frames():
    """Once folded, the reading persists through later frames even if the
    task stash is gone — the projection is cumulative (dict(previous))."""
    from lib.turn_lifecycle import _task_projection

    task = _task()
    previous = {'lastRoundUsage': {'round': 1, 'model': 'm', 'tag': 'R1',
                                   'tokensIn': 41000, 'tokensOut': 10}}
    projection = _task_projection(task, previous)
    assert projection['lastRoundUsage']['tokensIn'] == 41000


def test_task_projection_without_stash_has_no_key():
    from lib.turn_lifecycle import _task_projection

    projection = _task_projection(_task(), {})
    assert 'lastRoundUsage' not in projection
