"""lib/context_limits — Auto-learned per-(provider, model) context-window limits.

Tofu often routes the same logical model id through several providers (e.g.
``deepseek-v3.2`` may be served by Tencent, Baidu, Huawei, Doubao gateways).
Each provider may advertise a different context length even for an identical
upstream model — and our preset tables in :mod:`lib.tasks_pkg.compaction`
inevitably get some of these wrong.

This package corrects the preset error in BOTH directions, by learning from
real traffic:

* **Shrink** — when an LLM call fails with ``PromptTooLongError``, learn a
  smaller ceiling for that ``(provider_id, model)`` pair. Two sources:
  - *authoritative* — the gateway stated its own ``maximum context length is M``;
    we learn M directly and immediately (a literal ceiling, not a guess).
  - *inferred* — we only know the rejected request size N; we guess
    ``N * 0.95``. Because a single transient blip (a momentary mis-route to a
    smaller backend behind the same model id) must not permanently collapse a
    genuine 1M window, an inferred shrink that drops the limit by more than
    ``_BIG_DROP_FACTOR`` requires ``_REQUIRED_STRIKES`` consecutive overflow
    events (within ``_STRIKE_WINDOW_SEC``) before it is persisted.

* **Expand** — when an LLM call succeeds, look at the actual ``prompt_tokens``
  it accepted. If the call sent more tokens than our currently-known limit,
  bump the learned ceiling up to that observed count plus a small headroom.
  Expand entries are FLOOR-ONLY at resolution time
  (:func:`resolve_learned_context_limit` returns ``max(static, learned)``):
  an expand recorded when the static preset was smaller is stale history,
  and treating it as an absolute ceiling would pin the window below the
  true one forever (the compaction gate caps prompts below the pin, so no
  observation can ever climb out — the expand-side mirror of the
  shrink-starvation deadlock below, with no TTL escape since expand
  entries are permanent). Live instance: sankuai::kimi-k3 pinned at
  383,727 while kimi-k3's real window is 1M (2026-07-26).

**TTL self-heal.** Shrink entries are inherently uncertain (the rejection may
have been a transient gateway/route hiccup, and once a limit shrinks our own
compaction caps every prompt below it — so the expand path can NEVER observe
tokens above the wrong ceiling to correct it; see the deadlock note below).
Each *shrink* entry therefore carries a timestamp and is ignored (and lazily
dropped) once older than ``_SHRINK_TTL_DAYS``. On expiry we fall back to the
static preset; if the smaller window is real, the next overflow re-learns it
within one request (which ``reactive_compact`` recovers gracefully). *Expand*
entries are permanent — they are only ever corroborated by a real accepted
prompt, so they cannot be wrong in a way that hurts the user.

    The expand-starvation deadlock (why TTL, not "expand harder"): when a
    shrink drops the learned limit L, ``_usable_context(L)`` and the
    force-compact trigger cap every outgoing prompt well below L. So
    ``observed_tokens`` is structurally < L forever, and the expand condition
    ``observed > preset`` is unsatisfiable. Expand can never rescue a wrongful
    shrink — only the shrink side (gate + TTL) can.

Both paths persist to ``data/config/server_config.json``:
  - ``model_context_limits``      → ``{"<provider_id>::<model>": int, ...}`` —
    the plain int map (public surface read by ``routes/config.py`` + frontend).
  - ``model_context_limits_meta`` → ``{"<key>": {"ts": float, "source": str,
    "strikes": int}}`` — sidecar metadata driving TTL + the strike gate.

A single ``_context_limit_learned`` blob is surfaced inside ``usage`` so the
orchestrator can show a one-line SSE notice to the user.

Key shape: ``"<provider_id>::<model>"`` — falls back to the bare model name
when no provider is known so older callers keep working.

────────────────────────────────────────────────────────────────────────────
This module is a re-export **facade** over the ``_store`` / ``_lookup`` /
``_learn`` sub-modules. The three mutable state objects live HERE and nowhere
else — ``_lock``, ``_LEARNED`` and ``_META`` are defined once below, and every
sub-module reaches them through this module object at call time. That is what
makes ``s1 is s2`` hold for ``lib.context_limits._LEARNED`` vs
``from lib.context_limits import _LEARNED`` and lets the self-heal test
monkeypatch ``_LEARNED`` / ``_META`` / ``_persist`` on this module and have
every code path observe the change.
"""

import threading

from lib.log import get_logger

logger = get_logger(__name__)

# ── Storage helpers (pure) + bounds — re-exported from ._store ──────────
from lib.context_limits._store import (  # noqa: E402,F401
    _key,
    _load,
    _persist,
    _MIN_LEARNABLE,
    _MAX_LEARNABLE,
)

# ── Single, process-wide shared mutable state ───────────────────────────
# Defined ONCE here; ._store / ._lookup / ._learn all reach these through
# this module object at call time (single-instance invariant).
_lock = threading.Lock()
_LEARNED: dict[str, int] = {}
_META: dict[str, dict] = {}

_LEARNED, _META = _load()

# ── Read path — re-exported from ._lookup ───────────────────────────────
from lib.context_limits._lookup import (  # noqa: E402,F401
    lookup_learned_context_limit,
    resolve_learned_context_limit,
    _SHRINK_TTL_DAYS,
    _SHRINK_TTL_SEC,
)

# ── Write path — re-exported from ._learn ───────────────────────────────
from lib.context_limits._learn import (  # noqa: E402,F401
    learn_shrink_from_error,
    learn_expand_from_success,
    _register_strike,
    _clear_pending_strikes,
    _EXPAND_HEADROOM,
    _MIN_SHRINK_FACTOR,
    _BIG_DROP_FACTOR,
    _REQUIRED_STRIKES,
    _STRIKE_WINDOW_SEC,
)


__all__ = [
    'lookup_learned_context_limit',
    'resolve_learned_context_limit',
    'learn_shrink_from_error',
    'learn_expand_from_success',
]
