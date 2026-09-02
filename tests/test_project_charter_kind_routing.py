"""tests/test_project_charter_kind_routing.py — every commit declares its kind,
and the kind decides where the text LANDS (owner-directed 2026-07-28).

## The contract under test

  invariant — a binding rule constraining FUTURE decisions. Lands in the
              charter. REQUIRES a one-line `summary` (the rule itself); the
              per-turn injection renders ONLY summaries — the full text is
              the project_charter_read detail path.
  lesson    — a methodology experience note. Routed to PROJECT MEMORY with
              BM25 dedup (same-topic lessons fold into ONE living memory,
              owner directive: "append-only variant accumulation is the
              charter-side disease, don't bring it to the memory side").
              MUST NOT grow the charter.
  report    — a completion/rejection record. REJECTED with a pointer to
              JOURNAL.md. MUST NOT grow the charter.

## Why these assertions are shaped this way

Per the charter's own guard discipline: every test asserts an OBSERVABLE
RESULT (charter decision count, memory file contents, block contents) and
never an implementation constant. The dedup test calibrates against the REAL
BM25 scorer with real same-family/cross-topic texts — measured separation is
~36 vs 0 around the 3.0 threshold, so the threshold itself is not what is
being asserted.

NEUTER x2 both bite: short-circuit the memory route -> the "lesson reaches
memory" test goes red; make the injection headline fall through to full text
-> the "summary only" test goes red.
"""

from __future__ import annotations

import os
import shutil

import pytest

from tests._nc_harness import patch_restore as _patch_restore

pytestmark = pytest.mark.unit

TEST_OWNER_USER_ID = 1
pytest_plugins = ('tests._chat_sidecar',)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, '..'))
_CHARTER_SRC = os.path.join(ROOT, 'lib', 'conversations', 'project_charter.py')

_PROJ = '/tmp/tofu-kind-routing'

_RULE = ('凭证脱敏是 fail-closed 白名单,禁止改回按字段名排除')
_EVIDENCE_TAIL = '三个携密载体被连续漏掉(env → headers → url),每次都靠作者当时记得'
_FULL_DECISION = (
    f'{_RULE}。完整取证:{_EVIDENCE_TAIL}。默认从「暴露」翻成「丢弃」后第四个洞不会存在。')

_LESSON_A = ('守卫必须断言结果而非实现:行为守卫 MUST 断言「任何东西都不落进 logs/」'
             '这类结果,而不是断言某个私有常量等于某个路径;棘轮守卫锚在语义单元'
             '(AST 节点、函数跨度)而非行号或私有符号名。')
_LESSON_B = ('守卫失效新形态——扫描面残缺:新写任何扫描/遍历类守卫(AST 扫描、'
             '正则扫源码、遍历文件树),第一步是把实际扫到的样本量打印出来人工比对;'
             'NEUTER 必须打在扫描面的边缘形态(私有名/带前缀名/多行跨度)。')
_LESSON_C = ('MCP 传输判定必须显式 is_stdio():实测 `!= "sse"` 这类二值假设有四处,'
             '其中 registry.build_server_config 是真雷(会把 streamable-http 条目当'
             '本地命令拉起);新增传输类型时只需扩 REMOTE_TRANSPORTS。')


@pytest.fixture(autouse=True)
def _clean(chat_sidecar):
    shutil.rmtree(_PROJ, ignore_errors=True)
    os.makedirs(_PROJ, exist_ok=True)
    import tests._seed as seed
    seed.clear_records('project_charter')
    seed.clear_events()
    yield
    shutil.rmtree(_PROJ, ignore_errors=True)


@pytest.fixture(autouse=True)
def _stub_push(monkeypatch):
    monkeypatch.setattr('lib.agent_core.push.push_event', lambda *a, **k: None)


def _commit(flask_app, args):
    """Commit through the HUMAN route — the only remaining charter writer.

    RETARGETED 2026-07-30. This helper used to invoke the
    ``project_charter_commit`` AGENT tool, which was withdrawn when the charter
    became human-review-only. The STORAGE contract it exercised (kind + summary
    land on the decision; the injection renders the summary; index= reads one
    entry) is unchanged and still worth guarding, so those tests now drive
    ``commit_charter`` directly.

    The tool's POLICY layer — reject a missing kind, reject kind=report toward
    JOURNAL.md, route kind=lesson to project memory — went away WITH the tool.
    Those tests are reversed in place below to assert the refusal, and the
    lesson-dedup measurement they encoded is preserved in
    ``_route_lesson_to_memory``'s docstring against a future re-point of
    ``create_memory`` (tracked as follow-up debt, not folded in here).
    """
    from lib.conversations.project_charter import commit_charter
    kind = (args.get('kind') or '').strip().lower()
    if kind and kind != 'invariant':
        raise AssertionError(
            f'_commit only drives the invariant/storage path; kind={kind!r} '
            f'was a POLICY behaviour of the withdrawn agent tool')
    with flask_app.app_context():
        res = commit_charter(_PROJ, add_decision=args.get('decision'),
                             decision_kind='invariant',
                             summary=args.get('summary', ''),
                             updated_by_conv='conv-test',
                             resolves_proposal=args.get('resolves_proposal', ''), user_id=TEST_OWNER_USER_ID)
    if res.get('ok'):
        return f'committed to the charter (version {res.get("version")})'
    return f'Error: {res.get("error", "unknown")}'


def _agent_commit_attempt(flask_app, args):
    """Call the WITHDRAWN agent tool — used by the reversed policy tests."""
    from lib.conversations.project_charter import execute_charter_tool
    with flask_app.app_context():
        return execute_charter_tool('project_charter_commit', args,
                                    current_conv_id='conv-test',
                                    project_path=_PROJ, user_id=TEST_OWNER_USER_ID)


def _decisions(flask_app):
    from lib.conversations.project_charter import read_charter
    with flask_app.app_context():
        return read_charter(_PROJ, user_id=TEST_OWNER_USER_ID)['decisions']


def _injection(flask_app):
    from lib.conversations.project_charter import render_charter_injection_block
    with flask_app.app_context():
        return render_charter_injection_block(_PROJ, user_id=TEST_OWNER_USER_ID)


def _tool_read(flask_app):
    from lib.conversations.project_charter import execute_charter_tool
    with flask_app.app_context():
        return execute_charter_tool('project_charter_read', {},
                                    current_conv_id='conv-test',
                                    project_path=_PROJ, user_id=TEST_OWNER_USER_ID)


def _project_memories():
    from lib.memory.storage import list_memories
    return [m for m in list_memories(_PROJ, scope='project')
            if not m.get('is_package')]


# ── invariant path ───────────────────────────────────────────────────────

def test_invariant_commit_lands_in_charter_with_kind_and_summary(flask_app):
    resp = _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                               'summary': _RULE})
    assert 'committed' in resp.lower(), resp
    decs = _decisions(flask_app)
    assert len(decs) == 1
    assert decs[0].get('kind') == 'invariant'
    assert decs[0].get('summary') == _RULE
    assert decs[0]['text'] == _FULL_DECISION


def test_injection_renders_the_summary_not_the_evidence(flask_app):
    """The two-tier split (owner): the per-turn injection shows the RULE;
    the evidence chain stays in the tool detail path."""
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})
    inj = _injection(flask_app)
    assert _RULE in inj, 'the binding rule must be resident per-turn'
    assert _EVIDENCE_TAIL not in inj, (
        'the evidence chain leaked into the per-turn injection — the split '
        'is gone and the block will bloat back to 2k/entry')
    # The DEFAULT tool read mirrors the injection (headlines); the evidence
    # is one index= call away (owner directive ③).
    default_read = _tool_read(flask_app)
    assert _RULE in default_read
    assert _EVIDENCE_TAIL not in default_read, (
        'the default read must be the headline list, not the full charter')


def test_read_with_index_returns_one_entry_full_text(flask_app):
    """Owner directive ③: project_charter_read(index=N) returns ONLY that
    entry — the evidence chain costs one entry, not the whole charter."""
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})
    _commit(flask_app, {'kind': 'invariant',
                        'decision': '第二条决策:共享 HEAD 禁止 stash。',
                        'summary': '禁止 stash'})
    from lib.conversations.project_charter import execute_charter_tool
    with flask_app.app_context():
        out = execute_charter_tool('project_charter_read', {'index': 0},
                                   current_conv_id='conv-test',
                                   project_path=_PROJ, user_id=TEST_OWNER_USER_ID)
    assert _EVIDENCE_TAIL in out, 'index read must return the full evidence'
    assert _RULE in out, 'index read carries the entry summary as header'
    assert '第二条决策' not in out, 'index read must NOT drag in other entries'
    # Negative index counts from the end.
    with flask_app.app_context():
        out_neg = execute_charter_tool('project_charter_read', {'index': -1},
                                       current_conv_id='conv-test',
                                       project_path=_PROJ, user_id=TEST_OWNER_USER_ID)
    assert '第二条决策' in out_neg and _EVIDENCE_TAIL not in out_neg


def test_read_index_out_of_range_is_an_error_not_a_dump(flask_app):
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})
    from lib.conversations.project_charter import execute_charter_tool
    with flask_app.app_context():
        out = execute_charter_tool('project_charter_read', {'index': 9},
                                   current_conv_id='conv-test',
                                   project_path=_PROJ, user_id=TEST_OWNER_USER_ID)
    assert 'out of range' in out, out
    assert _EVIDENCE_TAIL not in out


def test_a_legacy_decision_without_summary_still_renders_a_headline(flask_app):
    """Entries from before the summary field must not wall-of-text the
    injection either: they abridge to a first-line headline."""
    from lib.conversations.project_charter import commit_charter
    with flask_app.app_context():
        commit_charter(_PROJ, add_decision=('旧条目第一行是规则本体\n'
                                            '第二行开始是两千字考古 ' + 'x' * 600),
                       updated_by_conv='agent', user_id=TEST_OWNER_USER_ID)
    inj = _injection(flask_app)
    assert '旧条目第一行是规则本体' in inj
    assert 'x' * 600 not in inj


def test_the_kind_policy_layer_went_away_with_the_agent_tool(flask_app):
    """REVERSED IN PLACE 2026-07-30 — replaces eleven tests of a withdrawn tool.

    The kind-routing POLICY (2026-07-28) lived inside the
    ``project_charter_commit`` agent tool: reject a commit with no ``kind``;
    reject ``kind=report`` toward JOURNAL.md; route ``kind=lesson`` to project
    memory with three-channel dedup; require ``summary`` on an invariant. All of
    it was reachable ONLY from that tool, and the tool was withdrawn because a
    charter always requires human review (owner-directed 2026-07-30).

    So the eleven tests that drove it are collapsed here rather than kept green
    against code no one can reach. What they asserted is recorded so it is not
    lost:

      * missing kind / missing summary / kind=report → rejected with guidance
      * kind=lesson → project memory, never the charter, folding same-topic
        variants into ONE memory (measured: real same-family lesson pairs score
        ~0.10 lexical containment, which is WHY the explicit ``into_memory``
        channel exists — a lexical threshold alone can never catch a semantic
        family)

    The lesson path's measurement survives in ``_route_lesson_to_memory``'s
    docstring, which is now unreachable and labelled as such. Agents still
    record lessons via ``create_memory``; re-pointing that helper at the dedup
    logic is follow-up debt, deliberately NOT folded into this change.

    What this test pins is the only thing still true: the tool refuses, and it
    refuses by NAME so the refusal cannot be mistaken for a crash."""
    for args in (
        {'decision': _FULL_DECISION, 'summary': _RULE},           # no kind
        {'kind': 'invariant', 'decision': _FULL_DECISION},        # no summary
        {'kind': 'report', 'decision': 'TTFT watchdog shipped.'},  # report
        {'kind': 'lesson', 'decision': _LESSON_A},                # lesson
    ):
        out = _agent_commit_attempt(flask_app, args)
        assert 'project_charter_propose' in out, (
            f'the refusal must name the surviving route; got: {out!r}')
        assert _decisions(flask_app) == [], (
            f'a withdrawn-tool call must never grow the charter; args={args!r}')

    # And nothing leaked into project memory either — the lesson route is gone,
    # not silently still-running.
    assert _LESSON_A not in '\n'.join(
        (m.get('body') or '') for m in _project_memories())


# ── Negative controls ────────────────────────────────────────────────────

def test_NC3_stripping_the_index_branch_breaks_per_entry_read(flask_app):
    """NEUTER the index branch (fall through to the default headline list)
    → an index= call no longer returns the entry's full evidence."""
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})

    def run(_mod=None):
        from lib.conversations.project_charter import execute_charter_tool
        with flask_app.app_context():
            out = execute_charter_tool('project_charter_read', {'index': 0},
                                       current_conv_id='conv-test',
                                       project_path=_PROJ, user_id=TEST_OWNER_USER_ID)
        assert _EVIDENCE_TAIL not in out, (
            'NC-3 did not bite: the evidence still returned after the index '
            'branch was neutered')

    _patch_restore(
        _CHARTER_SRC,
        "            idx = fn_args.get('index')\n"
        "            if idx is not None and idx != '':",
        "            idx = fn_args.get('index')\n"
        "            if False:  # NC-3: index branch stripped",
        run)


def test_NC2_falling_back_to_full_text_breaks_the_two_tier_split(flask_app):
    """NEUTER the summary preference in _decision_headline (fall through to
    full text) -> the evidence tail leaks into the injection."""
    _commit(flask_app, {'kind': 'invariant', 'decision': _FULL_DECISION,
                        'summary': _RULE})

    def run(_mod=None):
        inj = _injection(flask_app)
        assert _EVIDENCE_TAIL in inj, (
            'NC-2 did not bite: the headline still hides the evidence tail '
            'after the summary preference was neutered')

    _patch_restore(
        _CHARTER_SRC,
        "        summary = (d.get('summary') or '').strip()\n"
        "        if summary:\n"
        "            return summary",
        "        summary = (d.get('summary') or '').strip()\n"
        "        if summary:\n"
        "            pass  # NC-2: fall through to the full text",
        run)
