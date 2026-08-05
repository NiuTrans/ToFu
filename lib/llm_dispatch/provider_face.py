"""lib/llm_dispatch/provider_face.py — the account/face separation.

ONE provider entry describes ONE ACCOUNT. An account has:

  * credentials + policy — ``api_keys``, ``extra_headers``, billing, quota.
    Cardinality 1. This is what "a provider" means to a user.
  * one or more WIRE FACES — a ``(base_url, protocol)`` pair. Cardinality N.

Historically ``base_url`` and ``protocol`` lived only at the provider level,
so an account with two faces was inexpressible and had to be duplicated into
two provider cards sharing one set of keys. The your-llm-gateway.example.com gateway is
exactly that shape: ``/v1/openai/native`` (OpenAI Chat Completions) and
``/v1/anthropic`` (Anthropic Messages), same account, same keys. The
duplication was never a design choice — it was the only writable shape.

A provider now declares its alternates::

    {
      "base_url": "https://api.openai.com/v1",   # default face
      "faces": {
        "anthropic": {"base_url": "https://api.openai.com/v1/anthropic",
                      "protocol": "anthropic"}
      },
      "models": [ {"model_id": "kimi-k3"},                       # default face
                  {"model_id": "claude-opus-5"} ]                # → anthropic
    }

RESOLUTION ORDER
----------------
1. ``model['face']`` names a face explicitly → use it. The escape hatch, and
   the ONLY way to force a Claude model onto the default face.
2. Claude-family model + the provider declares an anthropic-protocol face →
   that face. Automatic, so a future ``opus-6`` / ``fable-6`` is correct the
   day it is added, with no config edit and no guard to remember.
3. Otherwise → the provider's default ``base_url`` / ``protocol``.

WHY RULE 2 FAILS LOUD INSTEAD OF FALLING BACK
---------------------------------------------
Never silently degrade a Claude model onto a non-Anthropic wire. Measured
2026-07-28 on ``yuju-claude-opus-5-evaDaily``: the OpenAI face streamed 111
chunks / 33 ``reasoning_content`` / **0 signature**, while the Anthropic face
delivered ``signature_delta`` intact. A thinking block replayed WITHOUT its
signature is rejected upstream, so which face a Claude model sits on is a
correctness fact, not a preference.

The concrete path that would re-introduce it: ``_syncFromTemplate`` ADDs every
template entry the provider lacks, but never writes provider-level fields. So
a merged template carrying the Claude roster + one click of "sync from
template" on an OLD card with no ``faces{}`` would land the whole roster on
the OpenAI face. Refusing to build those slots makes the breakage VISIBLE (the
model is missing and the log says why) instead of invisible (the model works
but its reasoning is silently unusable).

The refusal is HOST-SCOPED, and the host set is DERIVED from the shipped
templates rather than hand-listed — Bedrock, OpenRouter, shubiaobiao and
yeysai all legitimately serve Claude over OpenAI-compatible endpoints and
expose no Anthropic face; on those hosts the OpenAI wire is the only option
and refusing it would outlaw working configurations.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlparse

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'FaceResolution',
    'resolve_face',
    'dual_face_hosts',
    'merge_duplicate_account_faces',
    'account_namespace_map',
    'provider_faces',
    'DEFAULT_FACE',
]

DEFAULT_FACE = 'default'

_TEMPLATE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'static', 'provider_templates')

# Lazily-built {host} set of gateways known to offer an Anthropic-native face.
_DUAL_FACE_HOSTS: set | None = None


@dataclass(frozen=True)
class FaceResolution:
    """The wire face one model entry resolves to.

    Attributes:
        ok: False when the entry must NOT be dispatched (see ``error``).
        base_url: the URL to send to.
        protocol: '' / 'openai' / 'anthropic' / 'responses'.
        face_name: which declared face was chosen ('default' or a key of
            ``provider['faces']``).
        forced: True when an explicit ``model['face']`` pin overrode the
            family rule — the caller should surface this, since it is the one
            way a Claude model can legitimately sit on a non-Anthropic wire.
        error: human-readable refusal reason when ``ok`` is False.
    """
    ok: bool
    base_url: str = ''
    protocol: str = ''
    face_name: str = DEFAULT_FACE
    forced: bool = False
    error: str = ''


def _host(url: str) -> str:
    return (urlparse(url or '').hostname or '').lower()


def provider_faces(provider: dict) -> dict:
    """Return ``{face_name: {base_url, protocol}}`` including the default.

    The default face is always present under ``DEFAULT_FACE`` so callers can
    treat the map uniformly.
    """
    faces = {
        DEFAULT_FACE: {
            'base_url': provider.get('base_url') or '',
            'protocol': provider.get('protocol') or '',
        },
    }
    declared = provider.get('faces')
    if isinstance(declared, dict):
        for name, spec in declared.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                continue
            name = name.strip()
            if not name or name == DEFAULT_FACE:
                continue
            faces[name] = {
                'base_url': spec.get('base_url') or provider.get('base_url') or '',
                'protocol': spec.get('protocol') or '',
            }
    return faces


def _anthropic_face(faces: dict) -> str:
    """Name of the first anthropic-protocol face, or '' when none."""
    for name, spec in faces.items():
        if name == DEFAULT_FACE:
            continue
        if (spec.get('protocol') or '') == 'anthropic':
            return name
    if (faces.get(DEFAULT_FACE, {}).get('protocol') or '') == 'anthropic':
        return DEFAULT_FACE
    return ''


def dual_face_hosts(refresh: bool = False) -> set:
    """Hosts that are KNOWN to offer an Anthropic-native face.

    Derived from the shipped provider templates — a template declaring
    ``protocol: anthropic`` (or a ``faces`` entry that does) marks its host as
    dual-face. Deriving beats a hardcoded list: adding a gateway is then a
    data change, and a template edit can never leave the rule stale.
    """
    global _DUAL_FACE_HOSTS
    if _DUAL_FACE_HOSTS is not None and not refresh:
        return set(_DUAL_FACE_HOSTS)

    import json

    hosts: set = set()
    try:
        names = sorted(os.listdir(_TEMPLATE_DIR))
    except OSError as e:
        logger.warning('[Face] template dir unreadable (%s) — dual-face host '
                       'set is empty, Claude fail-loud disabled', e)
        _DUAL_FACE_HOSTS = hosts
        return set(hosts)

    for fname in names:
        if not fname.endswith('.json'):
            continue
        path = os.path.join(_TEMPLATE_DIR, fname)
        try:
            with open(path, encoding='utf-8') as f:
                tpl = json.load(f)
        except (OSError, ValueError) as e:
            logger.warning('[Face] skipping unreadable template %s: %s', fname, e)
            continue
        if not isinstance(tpl, dict):
            continue
        host = _host(tpl.get('base_url') or '')
        if not host:
            continue
        if _anthropic_face(provider_faces(tpl)):
            hosts.add(host)

    _DUAL_FACE_HOSTS = hosts
    logger.info('[Face] discovered %d dual-face gateway host(s): %s',
                len(hosts), sorted(hosts) or '—')
    return set(hosts)


def resolve_face(provider: dict, model_entry: dict,
                 dual_face_hosts: set | None = None) -> FaceResolution:
    """Resolve which wire face *model_entry* dispatches over.

    Args:
        provider: a ``config['providers'][i]`` dict.
        model_entry: one of its ``models`` entries.
        dual_face_hosts: override the derived host set (tests / callers that
            already computed it). ``None`` = derive from shipped templates.

    Returns:
        A :class:`FaceResolution`. When ``ok`` is False the caller MUST NOT
        build slots for this entry — see the module docstring.
    """
    from lib.llm_dispatch.model_entry import routing_group
    from lib.model_info._family import is_claude

    provider = provider if isinstance(provider, dict) else {}
    model_entry = model_entry if isinstance(model_entry, dict) else {}
    faces = provider_faces(provider)
    default = faces[DEFAULT_FACE]
    model_id = (model_entry.get('model_id') or '').strip()

    # ── 1. Explicit pin ──
    pinned = (model_entry.get('face') or '').strip()
    if pinned:
        spec = faces.get(pinned)
        if spec is None:
            return FaceResolution(
                ok=False,
                error=('model %r pins face %r, which provider %r does not '
                       'declare (known: %s)'
                       % (model_id, pinned, provider.get('id', '?'),
                          ', '.join(sorted(faces)))))
        return FaceResolution(
            ok=True, base_url=spec['base_url'], protocol=spec['protocol'],
            face_name=pinned, forced=True)

    # ── 2. Family rule: Claude belongs on the Anthropic wire ──
    # Keyed on the whole routing group (logical id ∪ wire pool), because the
    # id that actually goes on the wire is what the gateway sees — a logical
    # name can hide a Claude deployment and vice-versa.
    group = routing_group(model_entry) or ({model_id} if model_id else set())
    is_claude_entry = any(is_claude(mid) for mid in group)

    if is_claude_entry:
        anth = _anthropic_face(faces)
        if anth:
            spec = faces[anth]
            return FaceResolution(
                ok=True, base_url=spec['base_url'], protocol=spec['protocol'],
                face_name=anth)

        # No Anthropic face declared. On a gateway KNOWN to offer one, this
        # is the silent-degradation shape — refuse instead of stripping
        # thinking signatures invisibly.
        known = (dual_face_hosts if dual_face_hosts is not None
                 else globals()['dual_face_hosts']())
        if _host(default['base_url']) in known:
            return FaceResolution(
                ok=False,
                error=('Claude-family model %r on provider %r declares no '
                       'anthropic face, but its gateway (%s) offers one. '
                       'Dispatching over the OpenAI-compatible wire would '
                       'silently drop thinking-block signatures. Add '
                       'faces.anthropic to this provider (or re-apply the '
                       'template), or pin face="default" to override '
                       'deliberately.'
                       % (model_id, provider.get('id', '?'),
                          _host(default['base_url']))))

    # ── 3. Provider default ──
    return FaceResolution(
        ok=True, base_url=default['base_url'], protocol=default['protocol'],
        face_name=DEFAULT_FACE)


def _account_anchor(group: list) -> dict | None:
    """The card that survives a same-account fold: the first
    non-anthropic-protocol card of the group, or None when there is no
    anchor (a lone anthropic card has nothing to fold into).

    Shared by :func:`merge_duplicate_account_faces` and
    :func:`account_namespace_map` so the "which card wins" criterion lives
    in exactly one place.
    """
    for p in group:
        if isinstance(p, dict) and (p.get('protocol') or '') != 'anthropic':
            return p
    return None


def account_namespace_map(providers: list) -> dict:
    """Return ``{absorbed namespace: surviving account provider id}``.

    Key-health history (``lib.key_stats``) may have been recorded under a
    provider namespace that no longer names a Settings card:

      1. the conventional ``f'{account_id}_{face_name}'`` shape the
         historical duplicate face cards used (the YourProvider gateway's
         ``example_corp_anthropic`` = ``example-corp`` + the ``anthropic`` face) —
         covered whether or not the config merge already persisted;
      2. a duplicate card STILL present in config (the config merge has
         not run yet — e.g. first boot after the upgrade): the card's own
         id maps to its account anchor, by the SAME identity criterion
         :func:`merge_duplicate_account_faces` folds with.

    Consumed at key_stats load time to fold the history into the account
    namespace — a namespace with no card renders nowhere in Settings, and
    a PERSISTENT override orphaned there silently stops applying (the
    2026-07-29 invisible-outage: the anthropic face's three stopped keys
    showed 87–93% healthy green).
    """
    mapping: dict = {}
    if not isinstance(providers, list):
        return mapping

    for p in providers:
        if not isinstance(p, dict):
            continue
        pid = p.get('id')
        if not pid:
            continue
        for face_name in provider_faces(p):
            if face_name != DEFAULT_FACE:
                mapping[f'{pid}_{face_name}'] = pid

    by_account: dict = {}
    for p in providers:
        if isinstance(p, dict) and p.get('id'):
            by_account.setdefault(_account_identity(p), []).append(p)
    for (host, keys), group in by_account.items():
        if not host or not keys or len(group) < 2:
            continue
        anchor = _account_anchor(group)
        if anchor is None:
            continue
        for p in group:
            # Mirror merge_duplicate_account_faces exactly: only the
            # anthropic-protocol card of an account folds into the anchor.
            if p is anchor:
                continue
            if (p.get('protocol') or '') == 'anthropic':
                mapping[p['id']] = anchor['id']
    return mapping


def _account_identity(provider: dict) -> tuple:
    """What makes two provider entries THE SAME ACCOUNT.

    Host + the exact key SET. Keys are compared as a set because a user may
    reorder them freely, but a strict subset is a DIFFERENT account — two
    tenants of one gateway must never be merged (it would cross-wire their
    billing, quota and key-health history).
    """
    return (_host(provider.get('base_url') or ''),
            frozenset(k for k in (provider.get('api_keys') or [])
                      if isinstance(k, str) and k))


def merge_duplicate_account_faces(providers: list) -> bool:
    """Fold same-account provider cards that differ only by wire face.

    Before the account/face separation, an account exposing two wire
    protocols had to be written as two provider entries sharing one set of
    API keys (the your-llm-gateway.example.com gateway: ``/v1/openai/native`` +
    ``/v1/anthropic``). This collapses such a pair into ONE card whose
    non-default face lives in ``faces{}``.

    Mutates *providers* IN PLACE and returns True when anything changed, so
    the caller can persist once (mirrors ``_migrate_provider_extra_headers``).

    Merging requires SAME HOST **and** SAME KEY SET — see ``_account_identity``.
    An anthropic card with no OpenAI sibling is left untouched: there is
    nothing to merge into, and rewriting it would break a working config.
    """
    if not isinstance(providers, list) or len(providers) < 2:
        return False

    # Group by account, keeping list positions so we can rebuild in order.
    by_account: dict = {}
    for idx, p in enumerate(providers):
        if not isinstance(p, dict):
            continue
        by_account.setdefault(_account_identity(p), []).append(idx)

    drop: set = set()
    changed = False

    for (host, keys), idxs in by_account.items():
        if not host or not keys or len(idxs) < 2:
            continue

        group = [providers[i] for i in idxs]
        anchor = _account_anchor(group)
        secondaries = [(i, p) for i, p in zip(idxs, group)
                       if (p.get('protocol') or '') == 'anthropic']
        if anchor is None or not secondaries:
            continue  # nothing to fold, or nothing to fold INTO

        faces = anchor.get('faces')
        if not isinstance(faces, dict):
            faces = {}

        for si, sec in secondaries:
            faces['anthropic'] = {
                'base_url': sec.get('base_url') or '',
                'protocol': 'anthropic',
            }
            # Carry the roster over. Entries already present on the anchor
            # (matched on the logical id) are left as-is — the anchor is the
            # surviving card and its user edits win.
            have = {(m.get('model_id') or '')
                    for m in (anchor.get('models') or [])}
            for m in (sec.get('models') or []):
                if (m.get('model_id') or '') not in have:
                    anchor.setdefault('models', []).append(m)
            drop.add(si)
            changed = True

        if changed:
            anchor['faces'] = faces

    if not changed:
        return False

    survivors = [p for i, p in enumerate(providers) if i not in drop]
    providers[:] = survivors
    logger.info('[Face] merged %d duplicate account face card(s) into their '
                'primary provider', len(drop))
    return True
