"""tests/test_flow_visible_turn_translate.py — goal-mode per-turn translation.

Root cause this guards (proven 2026-08-29 on conv mtcrt05s turn 7a17881c):
after goal mode moved onto the FlowExecutor (worker/VU as sub-agent leaf
runs), translation of its visible turns was scheduled ONLY from the parent
task's terminal DONE event, reading a mutable task-side candidate list.
Two failure modes followed:

  1. A turn the terminal coordinator's snapshot missed stayed untranslated
     FOREVER — the production turn above is an English flow_node reply in a
     conversation with autoTranslate ON that has zero translation trace.
  2. Even when the coordinator worked, every intermediate worker/VU reply
     sat untranslated for the whole multi-minute run — and goal mode is a
     FRONTEND surface the human watches live, not an async background swarm.

The fix: ``schedule_settled_visible_turn_translations`` runs at the flow
turn-persistence boundary (each completed flow node), giving every settled
visible CHILD turn its own translation trigger; the terminal coordinator
keeps the root turn and skips per-turn-admitted children (no double spend).
Ordinary swarm sub-agents never pass through that boundary (no ``_turnId``),
so background sub-agent content stays untranslated and free.

Failing-first: the scheduler and the persistence-port hook do not exist on
the old shape, so every test here errors/fails before the fix.

Run::

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest \
        tests/test_flow_visible_turn_translate.py -v
"""

from __future__ import annotations

import types

import pytest

import lib.translate.terminal as terminal
from lib.orchestration_chat_turn_persistence import (
    OrchestrationChatTurnPersistence,
)

pytestmark = pytest.mark.unit


def _task(**overrides):
    task = {
        "id": "flowtask1",
        "convId": "conv-flow-1",
        "_turnId": "root-turn",
        "_userId": 7,
        "config": {"autoTranslate": True, "uiLang": "zh"},
        "_turnVisibleRunTurnIds": ["root-turn", "child-1"],
    }
    task.update(overrides)
    return task


def _english_turn(turn_id, **projection_overrides):
    projection = {"content": "All five corrections are applied and verified."}
    projection.update(projection_overrides)
    return {"status": "completed", "projection": projection,
            "turnId": turn_id}


def _patch_reads(monkeypatch, turns_by_id, *, lang_code="en"):
    """Stub the sidecar read + language detection the scheduler consumes."""
    # Import the commit owner before replacing turn_lifecycle.get_turn. A lazy
    # import during the patch would otherwise capture this lambda in commit's
    # module-level alias after monkeypatch restores the source module, leaking
    # the fake into later tests that share this process.
    from lib.translate import commit as translation_commit

    fake_get_turn = (
        lambda conv_id, turn_id, *, user_id: turns_by_id[turn_id]
    )
    monkeypatch.setattr(
        "lib.turn_lifecycle.get_turn",
        fake_get_turn,
    )
    monkeypatch.setattr(translation_commit, "get_turn", fake_get_turn)
    monkeypatch.setattr(
        "lib.text_lang.detect_language",
        lambda text, force_fasttext=False: types.SimpleNamespace(
            code=lang_code),
    )


def _patch_sinks(monkeypatch, calls):
    """Record whole-turn spawns / complete-marks / noop pushes."""
    monkeypatch.setattr(
        terminal,
        "_spawn_whole_turn_translation",
        lambda **kw: calls.append(("spawn", kw["turn_id"])),
    )
    monkeypatch.setattr(
        "lib.translate.commit.mark_turn_translation_complete",
        lambda conv_id, turn_id, *, user_id: calls.append(
            ("mark_complete", turn_id)),
    )
    monkeypatch.setattr(
        terminal,
        "_push_noop",
        lambda conv_id, turn_id, message_id, *, user_id: calls.append(
            ("noop", turn_id)),
    )


# ── 1. Scheduler: settled English child turn gets its own trigger ────────

def test_settled_english_child_turn_spawns_translation(monkeypatch):
    calls = []
    _patch_reads(monkeypatch, {
        "root-turn": _english_turn("root-turn"),
        "child-1": _english_turn("child-1"),
    })
    _patch_sinks(monkeypatch, calls)
    task = _task()

    admitted = terminal.schedule_settled_visible_turn_translations(task)

    # The ROOT turn is the running task's own output turn: it settles with
    # the terminal event and must NOT be eagerly scheduled (a translation
    # CAS must never race the live attempt's projection writes).
    assert admitted == 1
    assert ("spawn", "child-1") in calls
    assert ("spawn", "root-turn") not in calls
    assert task["_visibleTurnTranslationsScheduled"] == {"child-1"}


def test_already_target_child_turn_marks_complete_and_noops(monkeypatch):
    calls = []
    chinese = "所有五项修正均已完成并通过验证，没有遗留英文内容。"
    _patch_reads(monkeypatch, {
        "root-turn": _english_turn("root-turn"),
        "child-1": _english_turn("child-1", content=chinese),
    }, lang_code="zh")
    _patch_sinks(monkeypatch, calls)

    admitted = terminal.schedule_settled_visible_turn_translations(_task())

    assert admitted == 1
    assert ("mark_complete", "child-1") in calls
    assert ("noop", "child-1") in calls
    assert ("spawn", "child-1") not in calls


def test_chinese_opening_with_later_english_section_is_not_skipped(monkeypatch):
    """A dominant/first-200-char ``zh`` label cannot prove the whole answer
    is already translated. This is the production failure shape: a long
    Chinese opening hid a later English assistant section."""
    calls = []
    mixed = (
        "结论与排查过程如下。" * 30
        + "\n\n## Final implementation details\n"
        + "The terminal assistant content still requires translation. " * 20
    )
    _patch_reads(monkeypatch, {
        "child-1": _english_turn("child-1", content=mixed),
    }, lang_code="zh")
    _patch_sinks(monkeypatch, calls)

    admitted = terminal.schedule_settled_visible_turn_translations(_task())

    assert admitted == 1
    assert ("spawn", "child-1") in calls
    assert ("mark_complete", "child-1") not in calls
    assert ("noop", "child-1") not in calls


# ── 2. Gates ──────────────────────────────────────────────────────────────

def test_auto_translate_off_schedules_nothing(monkeypatch):
    calls = []
    _patch_reads(monkeypatch, {"child-1": _english_turn("child-1")})
    _patch_sinks(monkeypatch, calls)
    task = _task(config={"autoTranslate": False})

    assert terminal.schedule_settled_visible_turn_translations(task) == 0
    assert not calls


def test_swarm_sub_agent_shape_is_a_noop(monkeypatch):
    """A background swarm sub-agent task has no conversation-attempt turn
    identity — it must never pay for translation (the cost saving the
    per-turn trigger exists to preserve for async agents)."""
    calls = []
    _patch_reads(monkeypatch, {"child-1": _english_turn("child-1")})
    _patch_sinks(monkeypatch, calls)
    task = _task()
    del task["_turnId"]

    assert terminal.schedule_settled_visible_turn_translations(task) == 0
    assert not calls


# ── 3. Retry + idempotency semantics of the scheduled set ────────────────

def test_running_child_is_not_marked_scheduled_and_retried(monkeypatch):
    calls = []
    turns = {
        "child-1": {"status": "running",
                    "projection": {"content": "English draft"}},
    }
    _patch_reads(monkeypatch, turns)
    _patch_sinks(monkeypatch, calls)
    task = _task()

    assert terminal.schedule_settled_visible_turn_translations(task) == 0
    assert not calls
    assert not task.get("_visibleTurnTranslationsScheduled")

    # The turn settles later (next per-turn sync): it must still be eligible.
    turns["child-1"] = _english_turn("child-1")
    assert terminal.schedule_settled_visible_turn_translations(task) == 1
    assert ("spawn", "child-1") in calls


def test_second_sync_does_not_respawn(monkeypatch):
    calls = []
    _patch_reads(monkeypatch, {"child-1": _english_turn("child-1")})
    _patch_sinks(monkeypatch, calls)
    task = _task()

    assert terminal.schedule_settled_visible_turn_translations(task) == 1
    assert terminal.schedule_settled_visible_turn_translations(task) == 0
    assert calls == [("spawn", "child-1")]


def test_failed_spawn_stays_eligible_for_backstop(monkeypatch):
    """A spawn that raises must NOT mark the turn scheduled — the terminal
    coordinator remains the backstop and must be allowed to retry it."""
    _patch_reads(monkeypatch, {"child-1": _english_turn("child-1")})

    def _boom(**_kw):
        raise RuntimeError("translation runtime saturated")

    monkeypatch.setattr(terminal, "_spawn_whole_turn_translation", _boom)
    task = _task()

    assert terminal.schedule_settled_visible_turn_translations(task) == 0
    assert "child-1" not in task.get("_visibleTurnTranslationsScheduled", set())


# ── 4. Persistence-port integration: the hook fires after per-turn sync ──

def test_persistence_port_schedules_after_successful_sync(monkeypatch):
    order = []
    task = _task()

    def _store(_task, _turns):
        order.append("store")

    def _sync(_task, _turns):
        order.append("sync")
        return len(_turns) - 1

    scheduled = []
    monkeypatch.setattr(
        terminal,
        "schedule_settled_visible_turn_translations",
        lambda _task: scheduled.append(_task["id"]) or 1,
    )

    persistence = OrchestrationChatTurnPersistence(
        task, store_turns=_store, sync_turns=_sync)
    persistence.bind([{"role": "assistant", "content": "worker reply"}])

    assert persistence({"role": "assistant", "content": "worker reply"}) is True
    assert order == ["store", "sync"]
    assert scheduled == ["flowtask1"]


def test_persistence_port_skips_schedule_when_sync_fails(monkeypatch):
    def _store(_task, _turns):
        return None

    def _sync(_task, _turns):
        raise RuntimeError("sidecar down")

    scheduled = []
    monkeypatch.setattr(
        terminal,
        "schedule_settled_visible_turn_translations",
        lambda _task: scheduled.append(_task["id"]) or 1,
    )

    persistence = OrchestrationChatTurnPersistence(
        _task(), store_turns=_store, sync_turns=_sync)
    persistence.bind([{"role": "assistant", "content": "worker reply"}])

    assert persistence({"role": "assistant"}) is False
    assert not scheduled


# ── 5. Terminal coordinator: backstop without double spend ───────────────

def test_terminal_coordinator_skips_eagerly_scheduled_children(monkeypatch):
    """A child turn admitted by the per-turn boundary must not be translated
    a second time by the terminal coordinator; the root turn (never in the
    eager set) keeps its terminal handling."""
    calls = []
    turns = {
        "root-turn": _english_turn("root-turn"),
        "child-1": _english_turn("child-1"),
        "child-2": _english_turn("child-2"),
    }
    _patch_reads(monkeypatch, turns)
    _patch_sinks(monkeypatch, calls)
    monkeypatch.setattr(
        "lib.translate.incremental.submit_thinking_segment",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        "lib.translate.incremental.finalize_incremental",
        lambda _task, content: calls.append(("finalize", "root-turn"))
                               or True,
    )
    monkeypatch.setattr(
        "lib.translate.incremental.cancel_incremental", lambda _task: True)

    task = _task(
        _turnVisibleRunTurnIds=["root-turn", "child-1", "child-2"],
        _visibleTurnTranslationsScheduled={"child-1"},
    )
    terminal._translate_settled_turns(task)

    # child-1 was admitted per-turn → no duplicate spawn. child-2 was
    # missed by the eager pass → the backstop still catches it. The root
    # turn keeps its incremental-finalize handoff.
    assert ("spawn", "child-1") not in calls
    assert ("spawn", "child-2") in calls
    assert ("finalize", "root-turn") in calls


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
