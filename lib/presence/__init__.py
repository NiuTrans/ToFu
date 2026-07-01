"""lib.presence — cross-conversation live presence ("who is working here now").

The shared-document analog for Tofu: when several conversations (and long
autopilot runs) operate on the same project, each conversation is a *peer*
whose live activity — objective, current file, active/idle status, and the set
of files it has touched this run — is tracked here and broadcast so every other
conversation (and the user) can *see* it, like collaborator cursors in a shared
doc.

Architecture (see ``docs/PRESENCE.md`` once the slice lands):

  • **Authoritative state is in-memory** (single Hypercorn process is the
    contract), held in :mod:`lib.presence.registry` alongside the push hub.
  • **Write-through to disk** at ``<root>/.tofu/presence/registry.json`` via the
    atomic, per-path-locked :mod:`lib.json_store` (same discipline file-history
    uses) so the live state is crash-recoverable and human-inspectable. The
    disk copy is a MIRROR, never the source of truth.
  • **The backend computes every judgment.** ``status`` (active|idle), the human
    ``statusLabel``, and every conflict advisory string are formed server-side.
    The frontend is a decision-free renderer: it NEVER derives "active" from
    mere presence in the registry — only from the ``status`` word this layer
    puts on the wire (which itself is gated on a fresh heartbeat within TTL).
  • **Liveness without polling.** Heartbeats ride existing seams (task start,
    the ~5 s streaming checkpoint, each ``round_committed``, done). A peer that
    goes silent is transitioned active→idle→reaped by a single background
    sweep — no client polling, no per-token writes.
  • **Conflicts are notify-only.** :mod:`lib.presence.conflict` intersects the
    touched-file sets of active peers and emits an advisory; it never locks.
"""

from __future__ import annotations

from lib.presence.registry import (
    announce,
    depart,
    heartbeat,
    mark_idle,
    reconcile_on_startup,
    record_files,
    snapshot,
    start_sweeper,
    sweep,
)

__all__ = [
    'announce',
    'heartbeat',
    'record_files',
    'mark_idle',
    'depart',
    'sweep',
    'snapshot',
    'reconcile_on_startup',
    'start_sweeper',
]
