"""lib.presence.conflict — file-set overlap detection (notify-only).

Two active peers (conversations / autopilot runs) editing the SAME file in the
same project is the classic cross-conversation interference the presence layer
exists to surface. Per the locked design this is **notify-only**: we detect the
overlap and emit a fully-formed advisory so the user — and, since the registry
is readable in-context, the agent itself — can coordinate. We never take a
lock and never pause a run (fail-open).

This module is ALSO the measurement instrument for the Layer-3 ledger decision.
Every detected overlap is logged at INFO with a stable ``[presence-overlap]``
prefix carrying the root, the contended path, and the peers involved, so the
running system records real-world overlap frequency. We decide whether the
coordination layer (and the ledger) is worth more investment from THIS data,
not from synthetic pre-measurement.
"""

from __future__ import annotations

from lib.log import get_logger

logger = get_logger(__name__)


def _short(s: str, n: int = 8) -> str:
    return (s or '')[:n]


def _peer_key(peer: dict) -> str:
    """Composite identity of a peer: convId, or conv#agent for a sub-agent.

    Mirrors ``lib.presence.registry._peer_key`` so detection dedupes by the
    SAME identity the registry keys on — which is what makes two sub-agents of
    one conversation count as two distinct holders (the within-conversation
    worst case), not one.
    """
    conv = peer.get('convId') or ''
    agent = peer.get('agentId') or ''
    return f'{conv}#{agent}' if agent else conv


def _peer_label(peer: dict) -> str:
    """Human label for a peer in a conflict string (backend-formed, verbatim).

    A sub-agent reads as ``"<agentId> (in <parent title>)"`` so the user can
    tell WHICH sub-agent of WHICH conversation; a conversation reads as its
    title (or a short conv id).
    """
    agent = peer.get('agentId') or ''
    if agent:
        parent = peer.get('parentTitle') or peer.get('title') or ''
        return f'{agent} (in {parent})' if parent else agent
    return peer.get('title') or f'conv {_short(peer.get("convId") or "")}'


def detect_overlaps(peers: list[dict], *, exclude_key: str | None = None) -> list[dict]:
    """Return notify-only conflict advisories for files touched by 2+ PEERS.

    A "peer" is a conversation OR a sub-agent (composite identity
    ``convId``/``convId#agentId``). Detection is by peer identity, so two
    sub-agents of ONE conversation editing the same file ARE flagged exactly
    like two sibling conversations — the within-conversation worst case this
    feature exists to catch.

    Args:
        peers: the ACTIVE peer dicts for ONE root (caller filters by status +
            root). Each carries ``convId``, optional ``agentId`` (+ ``title`` /
            ``parentTitle``) and a ``files`` list (relative paths touched).
        exclude_key: when set (a composite peer key), only return advisories
            that INVOLVE this peer (so a freshly-writing peer is told about
            contention on files IT just touched). ``None`` returns every
            overlapping file across the active set (sweep / snapshot view).

    Returns:
        A list of fully-formed advisory dicts::

            {'path': 'lib/llm/stream.py',
             'peers': ['<key-a>', '<key-b>'],
             'message': 'X and Y are concurrently editing lib/llm/stream.py'}

        The ``message`` is the human-readable string the frontend renders
        verbatim — the frontend forms no conflict text of its own.
    """
    # path -> {peer_key: peer} of active peers touching it (deduped by identity).
    by_path: dict[str, dict[str, dict]] = {}
    for p in peers:
        key = _peer_key(p)
        if not key:
            continue
        seen_here: set[str] = set()
        for f in (p.get('files') or []):
            if not f or f in seen_here:
                continue
            seen_here.add(f)
            by_path.setdefault(f, {})[key] = p

    advisories: list[dict] = []
    for path, holders in by_path.items():
        # Distinct PEERS only — a single peer touching a file across rounds is
        # not a conflict. Two sub-agents of one conv ARE two distinct peers.
        if len(holders) < 2:
            continue
        keys = list(holders.keys())
        if exclude_key is not None and exclude_key not in keys:
            continue

        labels = [_peer_label(holders[k]) for k in keys]
        if len(labels) == 2:
            who = f'{labels[0]} and {labels[1]}'
        else:
            who = ', '.join(labels[:-1]) + f', and {labels[-1]}'
        message = f'{who} are concurrently editing {path}'

        advisories.append({
            'path': path,
            'peers': keys,
            'message': message,
        })
        # Telemetry instrument: one INFO line per detected overlap so the live
        # system records real overlap frequency (now incl. sub-agent overlaps,
        # greppable by the conv#agent key) for the ledger decision.
        logger.info('[presence-overlap] path=%s peers=%s', path, ','.join(keys))

    return advisories
