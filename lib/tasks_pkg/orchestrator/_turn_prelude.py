"""Turn prelude for human/swarm state, profiles, and cost experiments.

Extracted 2026-08-01 ( slice 33) from ``run_task``'s preamble.
Runs ONCE per invocation, after the VU-startup attribution and before
provider binding / config resolution. Returns the (possibly profile-
merged) cfg — the caller rebinds its local exactly as the inline
original did.

Steps (original order, each with its own branch):

1. **Conversation inbox slot revival.** A prior turn's swarm teardown
   (or abort) tombstones the conversation-keyed inbox slot on the
   premise that no orchestrator will ever drain it again. THIS task
   drains that same key every round, so the premise expires the moment
   a new orchestrator starts — without revival, operator steer silently
   degrades to the durable queue and the peer / background-command
   fast paths stay dark for the whole turn (2026-09-04 incident:
   swarm terminated at turn end, every later steer fell back to
   "queued" while the next turn kept running). Fail-soft, info-logged
   when a slot is actually revived.

2. **Swarm autocontinue chain reset on HUMAN turns.** A human-initiated
   turn (NOT carrying ``cfg['_swarmAutoContinue']``) means the user is
   back in the loop, so the consecutive-auto-continue ceiling starts
   fresh. Auto-continue turns carry the marker and must NOT reset (that
   is what bounds a runaway unattended loop). Fail-soft — a reset
   failure logs at debug and never blocks the turn. See
   ``lib/swarm/integration.py``.

3. **Capability profile merge.** Named profile defaults merge UNDER the
   explicit cfg (explicit caller values always win); no-op when cfg has
   no 'profile' key or selects the empty 'default'. Applied BEFORE model
   resolution + tool assembly so every downstream consumer sees the
   merged values. Rebinds ``task['config']`` too (the inline original
   did both).

4. **Cost experiment assignment.** When the server-side experiment is
   enabled, deterministically assigns the conversation and overlays only the
   bounded MCP-exposure / working-set policy. Disabled is an exact no-op;
   explicit request overrides are excluded rather than overwritten.

Browser routing is intentionally absent here. The selected client remains in
the request config until the browser tool handler constructs an explicit
owner/device runtime; no prelude may install ambient routing authority.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def run_turn_prelude(task, cfg, tid):
    """Run the preamble steps; return the (possibly merged) config.

    Args:
        task: Live task dict (``task['config']`` is updated by the
            profile merge, mirroring the inline original).
        cfg:  The task's config dict (``task['config']``).
        tid:  8-char task id prefix for log correlation.

    Returns:
        The cfg dict — the SAME object unless a non-default profile
        merged (then the merged dict, also stored to ``task['config']``).
    """
    # ── 1. Conversation inbox slot revival ──
    #    The tombstone only means "the previous drainer stopped"; this
    #    task IS the drainer now (drain_and_inject_inbox runs every round
    #    under the same swarm key), so a stale tombstone must not outlive
    #    task start. finalize re-tombstones at the end of any turn whose
    #    swarm is torn down / aborted, preserving the quiet-period guard.
    try:
        from lib import agent_inbox as _agent_inbox
        from lib.swarm.integration import swarm_key_for as _swarm_key_for
        _inbox_key = _swarm_key_for(task)
        if _agent_inbox.is_tombstoned(_inbox_key):
            _agent_inbox.untombstone(_inbox_key)
            logger.info('[Task %s] revived tombstoned inbox slot (key=%s)',
                        tid, _inbox_key)
    except Exception as _e:
        logger.debug('[Task %s] inbox slot revival failed: %s', tid, _e)

    # Rebuild the volatile delivery lane from its durable authority. This
    # runs once per real turn, including after a server restart; completion
    # alone never starts a synthetic model turn.
    # Transient and storage-free carriers own no durable recovery lane.
    if (task.get('convId') and task.get('_userId')
            and not task.get('_transientRuntime')
            and not task.get('_invocationStorageFree')):
        try:
            from lib import agent_inbox
            from lib.message_queue import pending_background_command_injections
            from lib.tasks_pkg.manager import task_user_id

            for receipt in pending_background_command_injections(
                    task['convId'], user_id=int(task_user_id(task))):
                agent_inbox.enqueue(
                    task['convId'], receipt['value'], priority='next',
                    mode='background-command',
                    extra={key: value for key, value in receipt.items()
                           if key != 'value'},
                )
        except Exception as exc:
            logger.warning('[Task %s] background receipt recovery deferred: %s',
                           tid, exc)

    # ── 2. Swarm auto-continue chain reset on HUMAN turns ──
    if not cfg.get('_swarmAutoContinue'):
        try:
            from lib.swarm.integration import (reset_autocontinue_chain,
                                                swarm_key_for)
            reset_autocontinue_chain(swarm_key_for(task))
        except Exception as _e:
            logger.debug('[Task %s] autocontinue chain reset failed: %s', tid, _e)

    # ── 3. Capability profile: merge named profile defaults UNDER the
    #    explicit cfg (explicit caller values always win) ──
    from lib.agent_core.profiles import apply_profile, resolve_profile_name
    _profile_name = resolve_profile_name(cfg)
    if _profile_name != 'default':
        cfg = apply_profile(cfg)
        task['config'] = cfg

    # ── 4. Conversation-sticky cost experiment. Fail open: observability
    #    must never become a reason an otherwise valid chat request fails.
    try:
        from lib.cost_experiments import apply_cost_experiment
        cfg = apply_cost_experiment(task, cfg)
    except Exception as _e:
        logger.error('[Task %s] cost experiment assignment failed; using '
                     'original request config: %s', tid, _e, exc_info=True)

    return cfg
