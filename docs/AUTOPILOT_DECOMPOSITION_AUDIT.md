# `lib/tasks_pkg/autopilot.py` decomposition audit

Owner board epic: **`pt_00459503f23b4c0e`** — "Decompose tasks_pkg/autopilot.py
(3375L) — largest tasks_pkg monolith". Epic description explicitly requires
"**coordinate with pt_8dc03017** (VU-as-independent-stream)".

## State snapshot (evidence live-collected 2026-07-23)

- **Actual size**: `2196 lines` (epic says 3375L; prior sibling slices already
  trimmed by ~35%). It is still the largest `tasks_pkg` file.
- **Top-level symbols**: 30 defs + 1 class (`_VUEventForwarder` at line 631).
- **Owner domain**: user profile says "Role: Lead dev for CAS/VU autopilot;
  orchestrates tool turns, P2P messaging, and exactly-once delivery." This
  file IS that domain's implementation.

## Proposed 4-module split (per epic description)

| Cluster | Target module | Symbols | Approx LOC |
|---|---|---|---|
| Marker r/w | `_markers.py` | `arm_autopilot`, `disarm_autopilot`, `_marker_exists`, `kick_autopilot`, `resume_armed_autopilot_after_crash`, `_store_run_record` | ~800 |
| Objective + budget | `_state.py` | `_extract_objective`, `_extract_objective_from_db`, `_get_or_persist_objective`, `_get_or_persist_run_id`, `_record_vu_turn_and_check_budget`, `_clear_run_id` | ~400 |
| Event forwarding | `_event_forwarding.py` | `_VUEventForwarder`, `_emit_vu_setup_phase`, `_emit_run_concluded_event`, `_emit_run_concluded` | ~200 |
| Baton handoff | `_baton.py` | `_presync_parent_reply`, `_has_pending_real_message`, `_successor_already_running`, `_append_vu_message_to_conv`, `_maybe_auto_translate_vu`, `_start_followup_task`, `conclude_run` | ~500 |
| VU decision | `_vu.py` | `run_virtual_user`, `maybe_run_autopilot`, `_run_autopilot_kick`, `is_autopilot_enabled` | ~800 |
| Resolvers | (fold into `_state.py`) | `_resolve_recent_run_id`, `_resolve_run_anchor_msgid` | ~100 |

`autopilot.py` becomes a re-export facade (~50 lines) preserving every
external import path.

## Blocker: `pt_8dc03017` owner-personal cutover coupling

The `pt_8dc03017` epic (VU-as-independent-stream, currently `[human-gated]`,
blocked 8×, owner-parked) explicitly mutates the following symbols from
autopilot.py that this decomposition would move:

| Symbol | pt_00459503 target | pt_8dc03017 planned change |
|---|---|---|
| `_VUEventForwarder` (class, line 631) | **→ `_event_forwarding.py`** | **DELETE** (step-3 cutover: "remove `_VUEventForwarder`") |
| `_autopilot_deciding` latch (referenced from `_finalize.py`) | (touched by any move) | **DELETE** (step-3 cutover: "remove the `_autopilot_deciding` latch") |
| VU `convId=''` opt-out (inside `run_virtual_user`) | **→ `_vu.py`** | **CHANGE** (step-3 cutover: "pull VU `convId=''` opt-out; VU under real `convId`") |
| `test_autopilot_poll_handoff.py` (guard) | (test-scope) | **DELETE** (step-3 cutover) |

**Every one of the four "coordinate points" is a mutation that lands ON THE
SAME SYMBOLS this decomposition would move to new files.** If pt_00459503 lands
first, the pt_8dc03017 cutover has to:
- Reach into `_event_forwarding.py` to delete a class it doesn't own
- Reach into `_vu.py` to change an opt-out it doesn't own
- Merge-conflict any un-imported symbol paths against the new facade

Conversely, if pt_8dc03017 lands first, this decomposition:
- Deletes files that no longer exist (`_event_forwarding.py` becomes empty)
- Has different symbol counts to move
- Can produce cleaner post-cutover boundaries (e.g. no `_event_forwarding.py`
  module at all, because the forwarder class is gone)

**The correct sequencing is pt_8dc03017 FIRST, pt_00459503 SECOND.** But
pt_8dc03017 is owner-parked (explicit sign-off required, "owner is Lead dev
for CAS/VU exactly-once delivery; explicitly parked on their own sign-off").

## Recommendation for a future dispatch

**When pt_8dc03017 lands** (any owner edit to `_finalize.py::_autopilot_deciding`
or `autopilot.py::convId=''` opt-out is the mechanical activation signal
already recorded on that epic), THIS epic can proceed cleanly:

1. Re-inventory the symbols (a couple will have been deleted by the cutover)
2. Extract in strangler-fig order: `_markers.py` → `_state.py` → `_baton.py` →
   `_vu.py` (deliberately doing `_event_forwarding.py` LAST, or skipping it
   if the cutover removed `_VUEventForwarder`)
3. Facade `autopilot.py` as re-export; preserve every `from
   lib.tasks_pkg.autopilot import X` call site via re-export attributes
4. Wire-parity test built on the same pattern as
   `tests/test_lib_orchestrator_wire_parity.py`

## Why this dispatch is NOT decomposing today

Under strict autonomous-dispatch discipline:

1. **Owner-personal domain**: user is Lead dev for CAS/VU exactly-once
   delivery. Their code, their sign-off, their surgical cutover parked
   awaiting explicit owner attention.
2. **Coupling ratio**: 4 of the 4 pt_8dc03017 cutover-touch-points are IN
   this file, on IN this decomposition's target-move list. The coupling
   is not "shares a file" — it is "moves the exact same symbols."
3. **Sequencing choice belongs to owner**: an autonomous dispatch that
   picks the wrong sequence forces the owner into a merge cleanup on
   their own domain code. The correct autonomous move is to NOT force
   that sequencing.

This is documented so a future dispatch (or the owner themselves) has a
concrete post-cutover work list rather than re-litigating the audit.
