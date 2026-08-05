"""Turn prelude — the pre-Section-1 human/swarm/profile/browser preamble.

Extracted 2026-08-01 (pt_03f4cdf1 slice 33) from ``run_task``'s preamble.
Runs ONCE per invocation, after the VU-startup attribution and before
provider binding / config resolution. Returns the (possibly profile-
merged) cfg — the caller rebinds its local exactly as the inline
original did.

Steps (original order, each with its own branch):

1. **Swarm autocontinue chain reset on HUMAN turns.** A human-initiated
   turn (NOT carrying ``cfg['_swarmAutoContinue']``) means the user is
   back in the loop, so the consecutive-auto-continue ceiling starts
   fresh. Auto-continue turns carry the marker and must NOT reset (that
   is what bounds a runaway unattended loop). Fail-soft — a reset
   failure logs at debug and never blocks the turn. See
   ``lib/swarm/integration.py``.

2. **Capability profile merge.** Named profile defaults merge UNDER the
   explicit cfg (explicit caller values always win); no-op when cfg has
   no 'profile' key or selects the empty 'default'. Applied BEFORE model
   resolution + tool assembly so every downstream consumer sees the
   merged values. Rebinds ``task['config']`` too (the inline original
   did both).

3. **Per-client browser routing.** ``cfg['browserClientId']`` sets the
   thread-local client ID so all browser commands (tools, fetch
   fallback, search fallback) from this task thread route to the
   correct device's extension.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def run_turn_prelude(task, cfg, tid):
    """Run the three preamble steps; return the (possibly merged) cfg.

    Args:
        task: Live task dict (``task['config']`` is updated by the
            profile merge, mirroring the inline original).
        cfg:  The task's config dict (``task['config']``).
        tid:  8-char task id prefix for log correlation.

    Returns:
        The cfg dict — the SAME object unless a non-default profile
        merged (then the merged dict, also stored to ``task['config']``).
    """
    # ── 1. Swarm auto-continue chain reset on HUMAN turns ──
    if not cfg.get('_swarmAutoContinue'):
        try:
            from lib.swarm.integration import (reset_autocontinue_chain,
                                                swarm_key_for)
            reset_autocontinue_chain(swarm_key_for(task))
        except Exception as _e:
            logger.debug('[Task %s] autocontinue chain reset failed: %s', tid, _e)

    # ── 2. Capability profile: merge named profile defaults UNDER the
    #    explicit cfg (explicit caller values always win) ──
    from lib.agent_core.profiles import apply_profile, resolve_profile_name
    _profile_name = resolve_profile_name(cfg)
    if _profile_name != 'default':
        cfg = apply_profile(cfg)
        task['config'] = cfg

    # ── 3. Per-client browser routing: thread-local client ID so all
    #    browser commands from this task thread route to the correct
    #    device's extension ──
    _browser_client_id = cfg.get('browserClientId')
    if _browser_client_id:
        from lib.browser import _set_active_client
        _set_active_client(_browser_client_id)
        logger.debug('[Task %s] Browser client routed to %s',
                     tid, _browser_client_id[:12])

    return cfg
