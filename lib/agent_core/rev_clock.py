"""Boot-anchored monotonic rev clock — the single mint for wire rev tuples.

Relocated from ``lib.conversations.meta_cache`` (2026-08-05): the mint is a
pure wall/monotonic CLOCK utility with zero persistence dependency, so it
belongs in the agent base — ``lib.agent_core.push`` (a CORE module) needs it
for the push connect snapshot, and importing ``lib.conversations.*`` from
core is exactly what the core/persistence boundary ratchet forbids
(tests/test_agent_core_boundary.py). ``meta_cache`` re-exports the mint for
its existing callers.

# ── pt_conv_state_ssot P1 / pt_781ae072d6ee4e84: monotonic rev tuple ──

runningTaskIdsRev on every notify frame is a two-element ``[ns, replica_id]``
JSON array so the client can idempotent-gate with a plain lex compare and
never accept a reordered stale frame:

  * ns          — BOOT-EPOCH-ANCHORED nanoseconds: a wall-clock anchor
                  sampled EXACTLY ONCE at import, advanced only by a
                  ``time.monotonic_ns()`` delta. See _BOOT_EPOCH_NS below.
  * replica_id  — reuses the ``TOFU_REPLICA_ID or pid`` convention already
                  in ``lib.agent_core.push.PushHub._replica_id`` so we do
                  NOT introduce a second replica-identity source.

⚠️  WHY NOT RAW ``time.monotonic_ns()`` (the original P1 choice, pt_781ae072)
----------------------------------------------------------------------------
monotonic_ns counts from THIS PROCESS's start. The client compares revs with
``>``, so a process-relative value is only meaningful against another value
from the SAME process. It produced three shipped failures, mirror images of
one another (all reproduced against the real reducer):

  * RESTART → permanent FALSE-BUSY. monotonic_ns resets to ~0, so every
    post-restart frame compares SMALLER than what connected tabs already
    hold and is dropped forever.
  * MULTI-REPLICA → starvation. A replica booted 10 days ago mints ~8.6e14;
    one booted an hour ago ~3.6e12, so the younger replica's frames can
    never land however recent they are.
  * Combined with the client's old wall-clock CLEAR stamp (~1.78e18) →
    permanent FALSE-IDLE on both transports until F5.

The anchor fixes all three at once because it puts every rev the fleet mints
— across processes and hosts — into ONE comparable domain: wall time.

REJECTED ALTERNATIVE: a persistent per-conv counter in the DB. Correct, but
it puts a write+read on the hot path of every notify frame (measured 135 us
median / 315 us p95 vs 0.19 us for the arithmetic — ~720x) and makes the DB
a hard dependency of the one signal that most needs to survive degraded
conditions. This scheme needs no storage at all.
"""

import os
import time

from lib.log import get_logger

logger = get_logger(__name__)

#: Wall-clock anchor for this process, sampled ONCE at import.
#:
#: ``time.time_ns() - time.monotonic_ns()`` is this process's estimate of the
#: wall-clock instant its monotonic clock read zero. Adding a later
#: ``monotonic_ns()`` to it therefore yields "wall time now" WITHOUT re-reading
#: the wall clock — which is what makes the result simultaneously:
#:
#:   * strictly increasing within the process (only the monotonic delta moves);
#:   * immune to a wall-clock STEP during the process (NTP slew/rewind cannot
#:     move an anchor that is never re-read);
#:   * comparable across restarts and across replicas (every process anchors
#:     to the same wall-clock timeline, to within NTP skew).
#:
#: Module-level and rebindable by name so tests can simulate a restart / a
#: second replica by re-anchoring; production never writes it.
_BOOT_EPOCH_NS = time.time_ns() - time.monotonic_ns()


def _replica_id() -> str:
    """Resolve THIS replica's stable id — same rule PushHub uses."""
    rid = os.environ.get('TOFU_REPLICA_ID')
    if rid:
        return rid
    return str(os.getpid())


def _running_task_ids_rev() -> list:
    """Return a fresh ``[boot_anchored_ns, replica_id_str]`` tuple.

    THE SINGLE MINT for every ``runningTaskIdsRev`` / frame-level ``rev`` on
    the wire (notify frames, the push connect snapshot, and the poll
    projection all call this). Keeping one mint is what makes the domain
    guarantee checkable in one place — see
    ``tests/test_conv_state_rev_clock_domain.py``, which asserts the returned
    ns is within seconds of ``time.time_ns()`` so a future edit cannot
    silently reintroduce a process-relative clock.

    Each call yields a strictly-later ns than the previous call in the same
    process (guaranteed by ``time.monotonic_ns()``; measured 0 non-increasing
    pairs in 200k consecutive mints at 1 ns clock resolution). Two callers on
    different replicas break ties by replica_id lex compare.
    """
    return [_BOOT_EPOCH_NS + time.monotonic_ns(), _replica_id()]


__all__ = ['_BOOT_EPOCH_NS', '_replica_id', '_running_task_ids_rev']
