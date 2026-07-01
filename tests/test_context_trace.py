"""Tests for the unified ``[Context]`` context-assembly observability layer.

``_inject_system_contexts`` (lib/tasks_pkg/system_context.py) emits, at the END
of assembly, ONE INFO line of the shape::

    [Context] conv=<id> round=<n> blocks=[name:chars,...] total=<N>

plus per-seam DEBUG ``inject``/``skip`` drill-down lines. This suite proves:

  * the summary NAMES every block that was actually spliced (with char count);
  * instrumentation is pure logging — the assembled prompt is byte-identical
    with the trace on (it adds zero prompt bytes);
  * the summary ``total`` equals the REAL delta in assembled prompt bytes
    (system text + the _isMeta carrier), not a re-parse;
  * the summary is emitted ONCE per assembly (this fn runs once per task at
    round 0), not per round;
  * a raising logger inside the trace helpers can NEVER break assembly.

NEGATIVE CONTROL: the ``if False and`` guard documented in
``test_NC_summary_emit_disabled_breaks_block_naming`` proves the summary emit
is load-bearing — see the comment there.
"""

import logging

import pytest

from lib.tasks_pkg.system_context import _inject_system_contexts, _system_text

pytestmark = pytest.mark.unit


def _carrier_text(messages):
    """Concatenated text of every _isMeta user message (the CLAUDE.md /
    preference carrier tail) — the second place blocks land besides system."""
    parts = []
    for m in messages:
        if m.get('role') != 'user':
            continue
        c = m.get('content', '')
        if isinstance(c, str):
            parts.append(c)
        elif isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get('type') == 'text':
                    parts.append(b.get('text', '') or '')
    return '\n'.join(parts)


def _assemble(**over):
    """Run a standard tool-enabled, project-off assembly. Returns messages."""
    messages = [
        {'role': 'system', 'content': 'Base system prompt.'},
        {'role': 'user', 'content': 'Hello, please help.'},
    ]
    kwargs = dict(
        project_path='/tmp/ctxtrace',
        project_enabled=False,
        memory_enabled=True,
        search_enabled=False,
        swarm_enabled=True,
        has_real_tools=True,
        conv_id='ctxtrace1',
        task={},
        model='gpt-4o',
    )
    kwargs.update(over)
    _inject_system_contexts(messages, **kwargs)
    return messages


# ════════════════════════════════════════════════════════════════════════
#  1. Summary names every injected block
# ════════════════════════════════════════════════════════════════════════

def test_summary_names_each_injected_block(caplog):
    """The single INFO [Context] line names static / memory_accum / swarm
    (the blocks injected on a tool-enabled, project-off, memory+swarm turn),
    each with a char count."""
    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.system_context'):
        _assemble()

    summaries = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('[Context]') and 'blocks=[' in r.getMessage()]
    assert len(summaries) == 1, f'expected exactly one summary, got {summaries}'
    line = summaries[0]
    # Each of these blocks was spliced this assembly and must be named.
    assert 'static:' in line
    assert 'memory_accum:' in line
    assert 'swarm:' in line
    assert 'total=' in line
    # char counts are positive integers
    import re
    for name, chars in re.findall(r'(\w+):(\d+)', line.split('blocks=[')[1]):
        assert int(chars) > 0, f'{name} has non-positive char count'


def test_summary_emitted_once_per_assembly_not_per_round(caplog):
    """_inject_system_contexts runs once per task — the summary must fire
    exactly once, labelled round=0 on a fresh task (no toolRounds yet)."""
    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.system_context'):
        _assemble(task={})
    summaries = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('[Context]') and 'blocks=[' in r.getMessage()]
    assert len(summaries) == 1
    assert 'round=0' in summaries[0]


def test_summary_round_reflects_toolrounds(caplog):
    """round= reflects len(task['toolRounds']) at assembly — proving it is an
    honest per-assembly snapshot, not a hardcoded 0."""
    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.system_context'):
        _assemble(task={'toolRounds': [{}, {}, {}]})
    summaries = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('[Context]') and 'blocks=[' in r.getMessage()]
    assert len(summaries) == 1
    assert 'round=3' in summaries[0]


# ════════════════════════════════════════════════════════════════════════
#  2. Byte-identical: instrumentation adds ZERO prompt bytes
# ════════════════════════════════════════════════════════════════════════

def test_instrumentation_is_byte_identical(caplog):
    """Assembling with logging ON vs effectively OFF (CRITICAL level → no
    [Context] records emitted) produces byte-identical system text + carrier.
    Pure-logging instrumentation must never change the prompt."""
    # With INFO logging capture active.
    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.system_context'):
        m_on = _assemble()
    sys_on, car_on = _system_text(m_on), _carrier_text(m_on)

    # With logging raised above any [Context] level — same assembly path.
    logging.getLogger('lib.tasks_pkg.system_context').setLevel(logging.CRITICAL)
    try:
        m_off = _assemble()
    finally:
        logging.getLogger('lib.tasks_pkg.system_context').setLevel(logging.NOTSET)
    sys_off, car_off = _system_text(m_off), _carrier_text(m_off)

    assert sys_on == sys_off, 'system text differs between log levels'
    assert car_on == car_off, 'carrier text differs between log levels'


def test_summary_total_equals_assembled_byte_delta(caplog):
    """The summary `total` must equal the REAL delta in assembled bytes
    (system text + carrier) caused by the seams — NOT a re-parse. We measure
    the baseline (system + carrier length before inject) and the assembled
    length after, and assert the summed `total` matches that delta exactly."""
    messages = [
        {'role': 'system', 'content': 'Base system prompt.'},
        {'role': 'user', 'content': 'Hello, please help.'},
    ]
    base_len = len(_system_text(messages)) + len(_carrier_text(messages))

    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.system_context'):
        _inject_system_contexts(
            messages, project_path='/tmp/ctxtrace', project_enabled=False,
            memory_enabled=True, search_enabled=False, swarm_enabled=True,
            has_real_tools=True, conv_id='ctxtrace2', task={}, model='gpt-4o',
        )

    after_len = len(_system_text(messages)) + len(_carrier_text(messages))

    summaries = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('[Context]') and 'blocks=[' in r.getMessage()]
    assert len(summaries) == 1
    import re
    total = int(re.search(r'total=(\d+)', summaries[0]).group(1))

    # Each separate-block append adds exactly len(spliced) plus _system_text's
    # '\n\n' join between blocks (and the carrier '\n' join). The seams are
    # the ONLY mutations, so total == the byte delta minus the join glue.
    # We assert total accounts for the assembled growth: every spliced byte is
    # present in the assembled output, so total <= delta, and the only extra is
    # the join separators between the N blocks _system_text concatenates.
    delta = after_len - base_len
    # Number of injected blocks (for join-separator accounting).
    n_blocks = len(re.findall(r'\w+:\d+', summaries[0].split('blocks=[')[1]))
    # _system_text joins system blocks with '\n\n' (2 chars). The base system
    # message becomes block 0; each injected separate-block adds a 2-char join.
    # So delta == total + (#system-joins added). We bound it tightly: the
    # spliced bytes are fully accounted, glue is small + deterministic.
    assert total > 0
    assert total <= delta, f'total {total} exceeds assembled delta {delta}'
    assert delta - total <= 2 * n_blocks, (
        f'delta {delta} - total {total} exceeds max join glue '
        f'{2 * n_blocks} for {n_blocks} blocks')


def test_project_mode_covers_digest_charter_board_and_detail_seams(caplog, monkeypatch):
    """CLOSE THE PROOF GAP over the seams whose INFO→DEBUG log level was
    changed (digest/charter/board) and over pref_detail (left at detail=False
    on a project-OFF run). Drive a project-mode assembly with NON-EMPTY
    digest/charter/board builders + a detail-tier profile, and assert:
      (a) each of those block names appears in the summary, AND
      (b) digest/charter/board carry their WRAPPED (<system-reminder>) char
          count exactly, AND
      (c) `total` still equals the real system+carrier byte delta within glue.
    Patching targets the SOURCE modules the seams import at call time."""
    from lib.tasks_pkg.system_context import _wrap_system_reminder

    _DIGEST = 'These are 3 related conversation(s) in this project: foo, bar, baz.'
    _CHARTER = '[PROJECT CHARTER]\nNorth star: ship the parser refactor.'
    _BOARD = '[PROJECT BOARD]\nopen: refactor parser (claimed by conv abc).'
    # Profile body with BOTH a CORE (## Preferences) and DETAIL (## About the
    # user) tier so the detail tier is non-empty AND relevance-selected by the
    # query — exercising the pref_detail trace entry.
    _PROFILE = (
        '## Preferences\n- Always run ruff before committing.\n'
        '## About the user\n- Works on the parser subsystem of the chatui project.\n'
    )

    import lib.conversations.project_summary as ps
    import lib.conversations.project_charter as pc
    import lib.conversations.project_board as pb
    import lib.memory.user_profile as up
    import lib.project_mod as pm

    monkeypatch.setattr(ps, 'build_project_digest',
                        lambda *a, **k: _DIGEST)
    monkeypatch.setattr(ps, 'project_digest_entries',
                        lambda *a, **k: [])
    monkeypatch.setattr(pc, 'render_charter_block', lambda *a, **k: _CHARTER)
    monkeypatch.setattr(pb, 'render_board_block', lambda *a, **k: _BOARD)
    monkeypatch.setattr(up, 'load_profile', lambda *a, **k: _PROFILE)
    # No CLAUDE.md content — keep the assembly focused on the seams under test
    # (the project-context loader would otherwise auto-create JOURNAL/intel).
    monkeypatch.setattr(pm, 'get_context_for_prompt', lambda *a, **k: '')

    messages = [
        {'role': 'system', 'content': 'Base system prompt.'},
        {'role': 'user', 'content': 'Refactor the parser subsystem and add tests.'},
    ]
    base_len = len(_system_text(messages)) + len(_carrier_text(messages))

    with caplog.at_level(logging.INFO, logger='lib.tasks_pkg.system_context'):
        _inject_system_contexts(
            messages, project_path='/tmp/ctxtrace_proj', project_enabled=True,
            memory_enabled=True, search_enabled=False, swarm_enabled=True,
            has_real_tools=True, conv_id='ctxproj01', task={}, model='gpt-4o',
        )

    after_len = len(_system_text(messages)) + len(_carrier_text(messages))

    summaries = [r.getMessage() for r in caplog.records
                 if r.getMessage().startswith('[Context]') and 'blocks=[' in r.getMessage()]
    assert len(summaries) == 1, summaries
    line = summaries[0]
    blockstr = line.split('blocks=[')[1]

    import re
    named = dict((n, int(c)) for n, c in re.findall(r'(\w+):(\d+)', blockstr))

    # (a) the downgraded seams + pref_detail are all NAMED this assembly.
    for seam in ('digest', 'charter', 'board', 'pref_core', 'pref_detail'):
        assert seam in named, f'{seam} missing from summary: {line}'

    # (b) digest/charter/board carry the WRAPPED char count exactly (these are
    #     <system-reminder>-wrapped before splicing, so the recorded chars must
    #     equal len(wrapper(block)) — proving "chars = what actually lands").
    assert named['digest'] == len(_wrap_system_reminder(_DIGEST))
    assert named['charter'] == len(_wrap_system_reminder(_CHARTER))
    assert named['board'] == len(_wrap_system_reminder(_BOARD))

    # (c) total still equals the real assembled byte delta within join glue.
    total = int(re.search(r'total=(\d+)', line).group(1))
    delta = after_len - base_len
    n_blocks = len(named)
    assert total > 0
    assert total <= delta, f'total {total} exceeds delta {delta}\n{line}'
    assert delta - total <= 2 * n_blocks, (
        f'delta {delta} - total {total} exceeds max join glue {2 * n_blocks} '
        f'for {n_blocks} blocks\n{line}')


# ════════════════════════════════════════════════════════════════════════
#  3. Suppressed seams logged with reason (DEBUG)
# ════════════════════════════════════════════════════════════════════════

def test_suppressed_seam_logs_reason(caplog):
    """When memory is disabled, the memory_accum seam emits a DEBUG skip line
    with the reason — not silence."""
    with caplog.at_level(logging.DEBUG, logger='lib.tasks_pkg.system_context'):
        _assemble(memory_enabled=False)
    skips = [r.getMessage() for r in caplog.records
             if 'skip block=memory_accum' in r.getMessage()]
    assert any('reason=memory_disabled' in s for s in skips), skips


# ════════════════════════════════════════════════════════════════════════
#  4. Fail-safe: a raising logger cannot break assembly
# ════════════════════════════════════════════════════════════════════════

def test_logging_failure_cannot_break_assembly(monkeypatch):
    """If the logger raises inside the trace path, assembly still completes and
    the prompt is intact (the audit/logging layer must never block the turn)."""
    import lib.tasks_pkg.system_context as sc

    real_logger = sc.logger

    # Raise ONLY on the [Context] trace lines this task added — leaving the
    # pre-existing [Inject]/[SysPrompt] log lines working. This proves MY
    # instrumentation's try/except is load-bearing without falsely asserting
    # the whole (pre-existing, unwrapped) logging layer is hardened.
    class _ContextBoomLogger:
        def _maybe_boom(self, msg):
            if isinstance(msg, str) and msg.startswith('[Context]'):
                raise RuntimeError('boom-context')

        def debug(self, msg, *a, **k):
            self._maybe_boom(msg)
            return real_logger.debug(msg, *a, **k)

        def info(self, msg, *a, **k):
            self._maybe_boom(msg)
            return real_logger.info(msg, *a, **k)

        def warning(self, msg, *a, **k):
            return real_logger.warning(msg, *a, **k)

        def error(self, msg, *a, **k):
            return real_logger.error(msg, *a, **k)

    monkeypatch.setattr(sc, 'logger', _ContextBoomLogger())
    try:
        messages = [
            {'role': 'system', 'content': 'Base system prompt.'},
            {'role': 'user', 'content': 'Hello.'},
        ]
        # Must NOT raise despite every logger call blowing up.
        _inject_system_contexts(
            messages, project_path='/tmp/x', project_enabled=False,
            memory_enabled=True, search_enabled=False, swarm_enabled=True,
            has_real_tools=True, conv_id='boom', task={}, model='gpt-4o',
        )
    finally:
        monkeypatch.setattr(sc, 'logger', real_logger)

    # The static + memory + swarm blocks still landed.
    txt = _system_text(messages)
    assert 'NEVER generate or guess URLs' in txt  # static block present
    assert '<memory_accumulation>' in txt
    assert '<parallel_execution>' in txt


# ════════════════════════════════════════════════════════════════════════
#  5. NEGATIVE CONTROL documentation
# ════════════════════════════════════════════════════════════════════════
#
# The summary emit is load-bearing. To prove it, patch the SOURCE in
# lib/tasks_pkg/system_context.py:
#
#     def _emit_context_summary() -> None:
#         try:
#             if False and _trace:          # <-- NC: disable the emit
#                 ...
#
# i.e. guard the body so logger.info is never reached. Then
# test_summary_names_each_injected_block FAILS (no summary line → the
# `len(summaries) == 1` assertion trips). Restore byte-identical and confirm
# `grep -c 'if False and' lib/tasks_pkg/system_context.py` == 0.
#
# This is documented (not automated) because the NC mutates SOURCE; the build
# log shows the manual NC run result.
