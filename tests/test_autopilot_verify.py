"""tests/test_autopilot_verify.py — Grounded virtual-user (driver) behavior.

Covers the "VU as project-owner driver" upgrade:
  • the immutable objective anchor extraction (``_extract_objective``);
  • the grounded TASK_DONE decision routed through the SINGLE source of
    truth ``lib.agent_verdict.classify_verdict(verifier_role='virtual_user')`` —
    a premature ``[VU: TASK_DONE]`` whose reply STILL flags unresolved work
    (❌ / "NOT met" / "still failing" / "unresolved") is downgraded to "keep
    going" so the autopilot loop refuses to stop while acceptance criteria are
    unmet;
  • ``run_virtual_user`` honoring that gate via a stubbed ``_run_single_turn``
    (no live LLM / orchestrator).

The gating logic itself is asserted to live ONLY in lib/agent_verdict.py — the
autopilot module just delegates.
"""

import pytest

from lib.agent_verdict import VU_DONE_SENTINEL, classify_verdict


# ── classify_verdict: virtual_user policy ──────────────────────────────

def test_vu_clean_done_stops():
    """A TASK_DONE with no unresolved markers ends the loop."""
    v = classify_verdict(
        'Verified: pytest ran green and the file is correct. ' + VU_DONE_SENTINEL,
        verifier_role='virtual_user')
    assert v['phase'] == 'stop'


def test_vu_premature_done_with_x_marker_downgraded():
    """TASK_DONE that still carries ❌ is downgraded to keep-going."""
    v = classify_verdict(
        'Tests still failing ❌ but the assistant says it is done. '
        + VU_DONE_SENTINEL,
        verifier_role='virtual_user')
    assert v['phase'] == 'worker'


def test_vu_premature_done_with_phrase_downgraded():
    """TASK_DONE whose body says a criterion is NOT met is downgraded."""
    v = classify_verdict(
        'The login flow is still NOT met. ' + VU_DONE_SENTINEL,
        verifier_role='virtual_user')
    assert v['phase'] == 'worker'


def test_vu_unresolved_phrase_downgraded():
    v = classify_verdict(
        'There are unresolved edge cases. ' + VU_DONE_SENTINEL,
        verifier_role='virtual_user')
    assert v['phase'] == 'worker'


def test_vu_plain_reply_keeps_going():
    """A plain instructional reply (no sentinel) means keep going."""
    v = classify_verdict('Next, please add a regression test for the parser.',
                         verifier_role='virtual_user')
    assert v['phase'] == 'worker'


def test_vu_empty_reply_keeps_going():
    """Empty VU output is a valid 'yeah, keep going' under autopilot."""
    v = classify_verdict('', verifier_role='virtual_user')
    assert v['phase'] == 'worker'


def test_vu_verdict_stop_tag_stops():
    """An explicit [VERDICT: STOP] (no unresolved markers) ends the loop."""
    v = classify_verdict('Objective met. [VERDICT: STOP]',
                         verifier_role='virtual_user')
    assert v['phase'] == 'stop'


# ── role prompt: driver + creativity + provenance mandate ──────────────

def test_vu_role_prompt_has_driver_and_creativity_and_provenance():
    """The VU prompt must encode: project-owner driver identity, mandatory
    verification, a creativity mandate (surface insights the agent missed),
    and the provenance contract (only the reply text reaches the agent)."""
    from lib.tasks_pkg.autopilot import _VU_ROLE_PROMPT as p
    low = p.lower()
    assert 'project owner' in low            # driver identity
    assert 'verify' in low                   # mandatory verification
    assert 'creativ' in low                  # creativity mandate
    assert 'not considered' in low or 'has not' in low  # surface missed insights
    assert 'provenance' in low               # provenance contract
    # The contract must make clear only the final reply is sent to the agent.
    assert 'reply' in low and 'self-contained' in low


# ── objective anchor extraction ────────────────────────────────────────

def test_extract_objective_first_real_user_msg():
    from lib.tasks_pkg.autopilot import _extract_objective
    msgs = [
        {'role': 'user', 'content': 'Build a CSV exporter that handles UTF-8.'},
        {'role': 'assistant', 'content': 'Sure, starting now.'},
        {'role': 'user', 'content': 'go on', '_isVirtualUser': True},
    ]
    assert _extract_objective(msgs) == 'Build a CSV exporter that handles UTF-8.'


def test_extract_objective_skips_vu_directive_and_synthetic():
    from lib.tasks_pkg.autopilot import _extract_objective
    msgs = [
        {'role': 'user', 'content': '=== role ===', '_isVuDirective': True},
        {'role': 'user', 'content': 'keep going', '_isVirtualUser': True},
        {'role': 'user', 'content': 'The real ask is here.'},
    ]
    assert _extract_objective(msgs) == 'The real ask is here.'


def test_extract_objective_multimodal_text_blocks():
    from lib.tasks_pkg.autopilot import _extract_objective
    msgs = [{'role': 'user', 'content': [
        {'type': 'text', 'text': 'Analyze'},
        {'type': 'image', 'source': {}},
        {'type': 'text', 'text': 'this chart.'},
    ]}]
    assert _extract_objective(msgs) == 'Analyze this chart.'


def test_extract_objective_empty_when_none():
    from lib.tasks_pkg.autopilot import _extract_objective
    assert _extract_objective([{'role': 'assistant', 'content': 'hi'}]) == ''
    assert _extract_objective([]) == ''


def test_extract_objective_skips_ismeta_context_carrier():
    """The runtime prepends a synthetic ``_isMeta`` user message (CLAUDE.md /
    user-preference profile) at index 0/1, BEFORE the human turn. The objective
    must be the human's ask, never that injected carrier — even though the
    carrier's content is a <system-reminder> wrapper (tag-agnostic: we skip by
    the flag, not by parsing the wrapper)."""
    from lib.tasks_pkg.autopilot import _extract_objective
    msgs = [
        {'role': 'user',
         'content': '<system-reminder>[USER PREFERENCE PROFILE] ...</system-reminder>',
         '_isMeta': True},
        {'role': 'user', 'content': 'The real human ask.'},
    ]
    assert _extract_objective(msgs) == 'The real human ask.'


def test_extract_objective_from_db_is_clean(monkeypatch):
    """``_extract_objective_from_db`` derives from the PERSISTED conversation
    (the source of truth for human input), which never carries injected
    context — so the pinned objective is the bare human ask regardless of how
    that per-turn context is wrapped in the live in-memory copy."""
    import lib.tasks_pkg.conv_message_builder as cmb
    import lib.tasks_pkg.autopilot as ap

    clean = [
        {'role': 'user', 'content': 'Fix the tablet case cutout question.'},
        {'role': 'assistant', 'content': 'a full answer'},
    ]
    monkeypatch.setattr(cmb, '_load_messages_from_db', lambda cid: clean)
    assert ap._extract_objective_from_db('conv-x') == \
        'Fix the tablet case cutout question.'
    # No conv id / empty DB → '' (caller falls back to the live list).
    assert ap._extract_objective_from_db('') == ''
    monkeypatch.setattr(cmb, '_load_messages_from_db', lambda cid: None)
    assert ap._extract_objective_from_db('conv-x') == ''


def test_vu_role_prompt_has_subjective_done_branch():
    """The VU prompt must instruct: a SUBJECTIVE / one-shot question already
    answered (nothing tool-checkable, no further criteria) is DONE — emit the
    sentinel instead of manufacturing filler / role-swapping into an assistant.
    This is the fix for the 'your message came through empty' churn loop."""
    from lib.tasks_pkg.autopilot import _VU_ROLE_PROMPT as p
    low = p.lower()
    assert 'subjective' in low or 'one-shot' in low
    assert 'nothing to verify' in low
    # It must route to the DONE sentinel, not to a follow-up.
    assert VU_DONE_SENTINEL in p


# ── run_virtual_user integration (stubbed sub-turn) ────────────────────

def _patch_subturn(monkeypatch, content):
    """Stub _run_single_turn so the VU sub-task 'replies' with `content`,
    and stub the objective resolver so no DB is touched."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    def _fake_turn(sub_task):
        sub_task['toolRounds'] = []
        return {'content': content}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Ship a working feature.')


def _vu_task():
    return {
        'id': 'task-vu-test-0001',
        'convId': 'conv-vu-test',
        'config': {'model': 'm', 'autopilot': True},
        'messages': [
            {'role': 'user', 'content': 'Ship a working feature.'},
            {'role': 'assistant', 'content': 'I think it is done.'},
        ],
    }


def test_run_vu_premature_done_keeps_going(monkeypatch):
    """VU says TASK_DONE but its reply still flags failing work → NOT a stop.

    run_virtual_user must return a real reply dict (keep going), NOT None, and
    the leftover sentinel token must be stripped from the fed-back text.
    """
    from lib.tasks_pkg.autopilot import run_virtual_user
    _patch_subturn(
        monkeypatch,
        'The tests are still failing ❌, this is not finished. '
        + VU_DONE_SENTINEL)

    task = _vu_task()
    result = run_virtual_user(task, vu_msg_id='vu-1')

    assert result is not None, 'premature TASK_DONE must NOT stop the loop'
    assert VU_DONE_SENTINEL not in result['text']
    assert 'still failing' in result['text']
    assert not task.get('_vu_emitted_done')


def test_run_vu_clean_done_stops(monkeypatch):
    """A verified, clean TASK_DONE stops the loop (returns None)."""
    from lib.tasks_pkg.autopilot import run_virtual_user
    _patch_subturn(
        monkeypatch,
        'Verified: the feature works and tests pass. ' + VU_DONE_SENTINEL)

    task = _vu_task()
    result = run_virtual_user(task, vu_msg_id='vu-2')

    assert result is None
    assert task.get('_vu_emitted_done') is True


def test_run_vu_plain_reply_keeps_going(monkeypatch):
    """A substantive instructional reply keeps the loop going."""
    from lib.tasks_pkg.autopilot import run_virtual_user
    _patch_subturn(
        monkeypatch,
        'Good progress. Next, add a regression test covering empty input.')

    task = _vu_task()
    result = run_virtual_user(task, vu_msg_id='vu-3')

    assert result is not None
    assert 'regression test' in result['text']
    assert not task.get('_vu_emitted_done')


def test_run_vu_injects_objective_anchor(monkeypatch):
    """The VU directive turn must carry the objective anchor header."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    captured = {}

    def _fake_turn(sub_task):
        captured['messages'] = sub_task.get('messages') or []
        sub_task['toolRounds'] = []
        return {'content': 'keep going'}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Build the UTF-8 CSV exporter.')

    ap.run_virtual_user(_vu_task(), vu_msg_id='vu-4')

    directive = captured['messages'][-1]
    assert directive.get('_isVuDirective') is True
    assert 'ORIGINAL OBJECTIVE' in directive['content']
    assert 'Build the UTF-8 CSV exporter.' in directive['content']
    # ── Stale-vs-live prompt marker: the directive must carry the
    #    content-derived version both in the visible text and as a dict key
    #    so a stale-process directive is mechanically identifiable.
    assert directive.get('_vuPromptVersion') == ap.VU_PROMPT_VERSION
    assert f'[prompt v{ap.VU_PROMPT_VERSION}]' in directive['content']


# ── segment timeline propagation (VU turn renders the agent inline timeline) ─

def _patch_subturn_with_rounds(monkeypatch, content, thinking, rounds):
    """Stub _run_single_turn so the finished VU sub-task leaves terminal
    content/thinking + a merged toolRounds list on the dict — exactly the state
    `assemble_segments` reads. Mirrors what a real `_run_single_turn` leaves
    behind (it does NOT assemble segments itself because the sub-task runs with
    `_endpoint_managed=True`, skipping the persist path)."""
    import lib.tasks_pkg.autopilot as ap
    import lib.tasks_pkg.orchestrator as orch

    def _fake_turn(sub_task):
        sub_task['content'] = content
        sub_task['thinking'] = thinking
        sub_task['toolRounds'] = rounds
        sub_task['finishReason'] = 'stop'
        return {'content': content, 'thinking': thinking}

    monkeypatch.setattr(orch, '_run_single_turn', _fake_turn)
    monkeypatch.setattr(ap, '_get_or_persist_objective',
                        lambda conv_id, msgs: 'Ship a working feature.')


def test_run_vu_returns_segments_for_inline_timeline(monkeypatch):
    """run_virtual_user must assemble + return the thin `segments` timeline off
    the finished sub-task so the VU turn renders the IDENTICAL agent inline
    per-tool timeline (owner directive). This is the ONLY assembly point — the
    sub-task runs `_endpoint_managed` and never assembled segments itself.

    Asserts the returned segments carry the interleaved shape (per-batch
    thinking + narration then the tool_use, then the terminal deliverable) and
    are THIN (no `_round` mirror — `toolRounds` is co-persisted)."""
    from lib.tasks_pkg.autopilot import run_virtual_user
    rounds = [
        {'toolCallId': 'tc1', 'toolName': 'read_files', 'status': 'done',
         'toolContent': 'ok', 'llmRound': 0, 'roundNum': 1,
         'assistantContent': 'Let me check the files.',
         'thinking': 'reason0'},
    ]
    _patch_subturn_with_rounds(
        monkeypatch,
        content='Good progress. Next, add a regression test.',
        thinking='terminal-reasoning',
        rounds=rounds)

    result = run_virtual_user(_vu_task(), vu_msg_id='vu-seg-1')
    assert result is not None
    segs = result.get('segments')
    assert isinstance(segs, list) and segs, 'VU result must carry segments'
    types = [s.get('type') for s in segs]
    # Interleaved: batch thinking + narration, then the tool_use, then the
    # terminal thinking + deliverable text.
    assert 'tool_use' in types, types
    tu = next(s for s in segs if s['type'] == 'tool_use')
    assert tu['id'] == 'tc1' and tu['name'] == 'read_files'
    # THIN form: the `_round` mirror must be stripped (toolRounds co-persisted).
    assert '_round' not in tu, 'segments must be persisted in THIN form'
    deliverables = [s for s in segs
                    if s['type'] == 'text' and s.get('deliverable')]
    assert len(deliverables) == 1, deliverables
    assert deliverables[0]['text'] == 'Good progress. Next, add a regression test.'


def test_run_vu_no_rounds_still_returns_segments_key(monkeypatch):
    """A VU turn with zero tool rounds (the common 'keep going' case) still
    returns a `segments` key (a list) — never a missing key — so the caller's
    `vu_result.get('segments') or []` is always well-defined. With no tools the
    list is just the terminal deliverable segment."""
    from lib.tasks_pkg.autopilot import run_virtual_user
    _patch_subturn_with_rounds(
        monkeypatch, content='Keep going.', thinking='', rounds=[])
    result = run_virtual_user(_vu_task(), vu_msg_id='vu-seg-2')
    assert result is not None
    assert isinstance(result.get('segments'), list)
    # No tools → no tool_use segments; exactly the terminal deliverable.
    assert all(s.get('type') != 'tool_use' for s in result['segments'])


# ── prompt version marker is content-derived ──────────────────────────

def test_vu_prompt_version_is_content_derived():
    """VU_PROMPT_VERSION is an 8-char hash of the prompt body — it must equal
    the hash of the CURRENT _VU_ROLE_PROMPT (so it auto-changes when the prompt
    text changes; a forgotten manual bump cannot drift)."""
    import hashlib
    import lib.tasks_pkg.autopilot as ap
    expected = hashlib.sha256(
        ap._VU_ROLE_PROMPT.encode('utf-8')).hexdigest()[:8]
    assert ap.VU_PROMPT_VERSION == expected
    assert len(ap.VU_PROMPT_VERSION) == 8
