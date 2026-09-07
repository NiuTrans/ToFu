"""tests/test_write_approval_gate.py — Write-approval guardrail contract.

Locks in the five-part hardening of the safety gate:

1. The approval set is DERIVED from the per-task write partition
   (``_write_tools``), not a hardcoded literal — so run_command, memory
   mutators, MCP write tools, and custom write tools are all approval-eligible.
2. ``run_command`` only prompts for DESTRUCTIVE commands; read-only commands
   (grep/ls/cat/git status) never block.
3. The auto-apply default is AUTO for ordinary writes (2026-08-21 policy): a task
   whose config omits autoApply never gates, attended or not. The gate fires
   only on an EXPLICIT per-conv Manual choice (autoApply=False) AND only for
   an attended task — an explicit Manual on a headless task still never
   blocks. (This replaced the old attended→Manual default, which deadlocked
   autonomous-dispatch turns: they ride the interactive chat lane, so they
   looked attended, omitted autoApply, and silently "rejected" every write
   after a 120s approval timeout nobody could answer.)
4. ToolSpec ``confirmation_tools`` gate even in Auto mode and reject
   unattended execution instead of waiting.
5. MCP tools are classified conservatively as WRITE unless their
   ``readOnlyHint`` annotation is explicitly True.

The pipeline's gate decision is replicated here as ``_would_gate`` mirroring
the exact predicate in ``execute_tool_pipeline`` so the test pins the policy
without spinning up a live task/LLM.
"""

from __future__ import annotations

import time

from lib.tasks_pkg.tool_dispatch._flags import (
    _IDEMPOTENT_TOOLS, _WRITE_TOOLS, _task_confirmation_tools,
    _task_partitions,
)


def _resolve_auto_apply(cfg, attended):
    """Mirror execute_tool_pipeline's auto-apply default (None → AUTO)."""
    auto_apply = cfg.get('autoApply')
    if auto_apply is None:
        auto_apply = True
    return auto_apply


def _would_gate(fn_name, fn_args, *, cfg, attended, write_tools, aborted=False,
                is_code_exec=False, confirmation_tools=frozenset()):
    """Replicate the pipeline's needs_approval predicate exactly."""
    auto_apply = _resolve_auto_apply(cfg, attended)
    requires_confirmation = fn_name in confirmation_tools and not aborted
    if requires_confirmation and not attended:
        return False
    needs_approval = requires_confirmation or (
        fn_name in write_tools
        and attended and not auto_apply and not aborted
        and not is_code_exec
    )
    if needs_approval and fn_name == 'run_command':
        from lib.project_mod.command_analysis import _is_destructive_command
        needs_approval = _is_destructive_command(fn_args.get('command', ''))
    return needs_approval


# ── 1. Gate derives from the write partition ────────────────────────

class TestGateDerivesFromPartition:
    def test_project_write_tools_gate_when_manual_attended(self):
        for tool in ('write_file', 'apply_diff', 'apply_diffs',
                     'insert_content', 'insert_contents'):
            assert _would_gate(tool, {'path': 'x.py'}, cfg={'autoApply': False},
                                attended=True, write_tools=_WRITE_TOOLS), tool

    def test_memory_mutators_gate_when_manual_attended(self):
        # Memory mutators are in the base write partition → now gated too.
        for tool in ('create_memory', 'update_memory', 'delete_memory',
                     'merge_memories'):
            assert _would_gate(tool, {}, cfg={'autoApply': False},
                               attended=True, write_tools=_WRITE_TOOLS), tool

    def test_readonly_tools_never_gate(self):
        for tool in ('read_files', 'grep_search', 'list_dir', 'find_files',
                     'web_search', 'fetch_url'):
            assert not _would_gate(tool, {}, cfg={'autoApply': False},
                                   attended=True, write_tools=_WRITE_TOOLS), tool

    def test_custom_write_tool_gates_via_partition(self):
        # A custom env write tool flows into _task_partitions and is gated.
        write = frozenset(_WRITE_TOOLS | {'custom__deploy'})
        assert _would_gate('custom__deploy', {}, cfg={'autoApply': False},
                           attended=True, write_tools=write)


# ── 2. run_command gates only when destructive ──────────────────────

class TestRunCommandGate:
    def test_destructive_run_command_gates(self):
        for cmd in ('rm foo.py', 'echo hi > config.json', "sed -i 's/a/b/' x",
                    'git reset --hard', 'python destructive.py'):
            assert _would_gate('run_command', {'command': cmd},
                               cfg={'autoApply': False}, attended=True,
                               write_tools=_WRITE_TOOLS), cmd

    def test_readonly_run_command_does_not_gate(self):
        for cmd in ('grep -r TODO src/', 'ls -la', 'cat README.md',
                    'git status', 'git log --oneline'):
            assert not _would_gate('run_command', {'command': cmd},
                                   cfg={'autoApply': False}, attended=True,
                                   write_tools=_WRITE_TOOLS), cmd


# ── 3. Auto-by-default ──────────────────────────────────────────────

class TestAutoApplyDefault:
    def test_attended_defaults_to_auto(self):
        # No autoApply key + attended → AUTO (no gate). This is the
        # autonomous-dispatch regression pin: brain/queued turns ride the
        # interactive chat lane (attended) with a config that omits
        # autoApply — they must NOT block on an approval nobody answers.
        assert _resolve_auto_apply({}, attended=True) is True
        assert not _would_gate('write_file', {'path': 'x'}, cfg={},
                               attended=True, write_tools=_WRITE_TOOLS)

    def test_unattended_defaults_to_auto(self):
        # No autoApply key + unattended (headless) → auto (never gates).
        assert _resolve_auto_apply({}, attended=False) is True
        assert not _would_gate('write_file', {'path': 'x'}, cfg={},
                               attended=False, write_tools=_WRITE_TOOLS)

    def test_explicit_manual_gates_when_attended(self):
        # The ONLY gating shape left: user explicitly switched this conv to
        # Manual AND a human is present to answer.
        assert _resolve_auto_apply({'autoApply': False}, attended=True) is False
        assert _would_gate('write_file', {'path': 'x'},
                           cfg={'autoApply': False}, attended=True,
                           write_tools=_WRITE_TOOLS)

    def test_unattended_never_gates_even_if_manual_requested(self):
        # Even an explicit autoApply=False cannot make a headless task block.
        assert not _would_gate('write_file', {'path': 'x'},
                               cfg={'autoApply': False}, attended=False,
                               write_tools=_WRITE_TOOLS)

    def test_explicit_auto_apply_overrides_attended_default(self):
        assert not _would_gate('write_file', {'path': 'x'},
                               cfg={'autoApply': True}, attended=True,
                               write_tools=_WRITE_TOOLS)


# ── 4. Always-confirm writes ─────────────────────────────────────────

class TestAlwaysConfirmWrites:
    def test_skill_install_gates_in_attended_auto_mode(self):
        confirmation = _task_confirmation_tools({})
        write, _idem = _task_partitions({})
        assert 'request_skill_install' in confirmation
        assert 'request_skill_install' in write
        assert _would_gate(
            'request_skill_install', {'catalog_id': 'skill-creator'},
            cfg={'autoApply': True}, attended=True, write_tools=write,
            confirmation_tools=confirmation)

    def test_skill_install_unattended_does_not_enter_wait_gate(self):
        confirmation = _task_confirmation_tools({})
        write, _idem = _task_partitions({})
        assert not _would_gate(
            'request_skill_install', {'catalog_id': 'skill-creator'},
            cfg={'autoApply': True}, attended=False, write_tools=write,
            confirmation_tools=confirmation)

    def test_approval_receipt_is_exact_and_single_use(self):
        from lib.tasks_pkg.tool_dispatch._approval import (
            consume_approval_receipt,
        )
        from lib.tasks_pkg.tool_dispatch._flags import _call_id_signature

        args = {'catalog_id': 'skill-creator', 'reason': 'needed'}
        task = {'_tool_approval_receipts': {
            'tc1': {
                'signature': _call_id_signature(
                    'request_skill_install', args),
                'minted_at': time.time(),
            },
        }}
        assert not consume_approval_receipt(
            task, 'request_skill_install', 'tc1',
            {'catalog_id': 'skill-creator', 'reason': 'changed'})
        # A mismatch consumes the receipt fail-closed.
        assert not consume_approval_receipt(
            task, 'request_skill_install', 'tc1', args)

        task['_tool_approval_receipts']['tc2'] = {
            'signature': _call_id_signature('request_skill_install', args),
            'minted_at': time.time(),
        }
        assert consume_approval_receipt(
            task, 'request_skill_install', 'tc2', args)
        assert not consume_approval_receipt(
            task, 'request_skill_install', 'tc2', args)

        task['_tool_approval_receipts']['expired'] = {
            'signature': _call_id_signature('request_skill_install', args),
            'minted_at': time.time() - 301,
        }
        assert not consume_approval_receipt(
            task, 'request_skill_install', 'expired', args)


# ── 5. MCP conservative classification ──────────────────────────────

class TestMCPClassification:
    def test_partition_treats_non_readonly_mcp_tool_as_write(self, monkeypatch):
        class _FakeBridge:
            connected = True

            def get_tool_safety(self):
                return {
                    'mcp__srv__delete_thing': False,   # write
                    'mcp__srv__list_things': True,      # read-only hint
                }

        import lib.mcp as mcp_mod
        monkeypatch.setattr(mcp_mod, 'get_bridge', lambda: _FakeBridge())

        write, idem = _task_partitions({})
        assert 'mcp__srv__delete_thing' in write
        assert 'mcp__srv__list_things' not in write
        # Base sets still fully contained.
        assert _WRITE_TOOLS <= write
        assert _IDEMPOTENT_TOOLS <= idem

    def test_no_bridge_returns_base_partition(self):
        # No MCP bridge connected → base partition unchanged.
        assert _task_partitions({}) == (_WRITE_TOOLS, _IDEMPOTENT_TOOLS)


if __name__ == '__main__':
    import sys

    import pytest
    sys.exit(pytest.main([__file__, '-v']))
