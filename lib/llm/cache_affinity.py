"""Prompt-cache affinity: one lineage identity, provider-declared carriers.

A prompt cache lives in one cache domain, and every hop between Tofu and the
model picks a domain for each request: Tofu picks an API key
(``lib/llm_dispatch/conv_affinity.py``), a gateway picks an upstream account,
a provider picks a serving shard. A hop that picks per request spreads one
conversation's rounds over several domains. The request bytes are identical,
but a round that lands on a new domain reads nothing back and writes the
whole prompt again.

A sequence of requests whose prompts extend each other is one *lineage*: a root
conversation, one child invocation, or one auxiliary series such as L2
summaries. Tofu derives one stable, non-identifying key per lineage and gives
it to every hop that documents an affinity carrier:

* Responses ``prompt_cache_key`` body field (route feature
  ``responses_prompt_cache``), serialized by ``lib.llm.responses_outbound``.
* Codex ``session-id`` / ``thread-id`` / ``x-client-request-id`` headers,
  set by ``lib.oauth.outbound``.
* Gateway headers declared below (route features ``cache_affinity_*``),
  added by :func:`merge_cache_affinity_headers` wherever outbound model
  headers are assembled (``lib.llm._sse_core.prepare_request`` for streaming,
  ``lib.llm.chat`` for non-streaming requests).

To support a new provider, add one ``CACHE_AFFINITY_HEADER_CARRIERS`` entry and
its source, register the feature in ``lib.llm.route_features``, and declare the
feature on that provider template's face. A carrier header the owner configured
in ``extra_headers`` wins, compared case-insensitively.

Entry points: :func:`cache_affinity_key`, :func:`merge_cache_affinity_headers`,
:func:`cache_affinity_evidence`.
Dependencies: ``lib.llm.route_features`` (declared route capabilities).
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping

#: Private request-body field naming the lineage. The agent round builder
#: sets it to ``lib.tasks_pkg.invocation.invocation_affinity_id``.
#: ``strip_internal_request_fields`` removes it before provider JSON.
CACHE_AFFINITY_SCOPE_KEY = '_cache_affinity_scope'

#: Route feature -> HTTP header carrying the lineage key. Each entry is a
#: provider contract and cites its source.
CACHE_AFFINITY_HEADER_CARRIERS = {
    # FRIDAY gateway (your-llm-gateway.example.com): each model is served by several
    # accounts and the cache is per account. The gateway hashes Mt-Session-Id
    # to pick the vendor, then the account; without it consecutive rounds
    # land on different accounts. 学城 2762588777 《Claude 模型接入推荐实践》 §5.1.
    'cache_affinity_mt_session_id': 'Mt-Session-Id',
}

#: Carriers whose hop picks the upstream cache domain from the lineage key
#: alone, whatever credential Tofu sent. FRIDAY's account pool does not depend
#: on the caller's AppId: on 2026-11-03 Claude rounds sent right after a
#: rebind to a second AppId read the entry the first AppId wrote, and back.
CACHE_DOMAIN_CARRIERS = frozenset({'mt-session-id'})


def affinity_selects_cache_domain(evidence: str) -> bool:
    """Whether ``cache_affinity_evidence`` names a domain-selecting carrier."""
    slots = evidence.split('@', 1)[0].split(',') if evidence else ()
    return any(slot.lower() in CACHE_DOMAIN_CARRIERS for slot in slots)


def cache_affinity_key(body: Mapping) -> str:
    """Return the request lineage's stable key, or '' when it has no lineage.

    The lineage is the explicit scope if set, else the body's conversation
    (``_conv_id``; auxiliary series such as L2 summaries set their own),
    else its task. The value is a UUID-shaped SHA-256 digest of it, so it is
    stable across rounds, turns, retries and fallbacks but reveals no Tofu
    identifier. Codex sends the same UUID shape in ``prompt_cache_key`` and
    its session headers. The ``tofu-responses-cache:`` salt predates this
    module. Changing it would rotate every live key and cost each in-flight
    lineage one full cache write.
    """
    scope = (body.get(CACHE_AFFINITY_SCOPE_KEY) or body.get('_conv_id')
             or body.get('_task_id') or '')
    if not scope:
        return ''
    digest = hashlib.sha256(
        f'tofu-responses-cache:{scope}'.encode('utf-8')).digest()[:16]
    return str(uuid.UUID(bytes=digest, version=5))


def cache_affinity_headers(profile, *, protocol: str, oauth: str,
                           key: str) -> dict[str, str]:
    """Headers that carry *key* on a route whose profile declares a carrier."""
    if not key:
        return {}
    from lib.llm.route_features import route_feature
    return {
        header: key
        for feature, header in CACHE_AFFINITY_HEADER_CARRIERS.items()
        if route_feature(profile, feature, protocol=protocol,
                         oauth=oauth) is True
    }


def merge_cache_affinity_headers(headers: dict, profile, *, protocol: str,
                                 oauth: str, key: str) -> None:
    """Add the route's declared carriers to the outbound *headers* in place.

    An owner-configured header with the same name, in any case, is kept.
    """
    present = {str(name).lower() for name in headers}
    for name, value in cache_affinity_headers(
            profile, protocol=protocol, oauth=oauth, key=key).items():
        if name.lower() not in present:
            headers[name] = value


def cache_affinity_evidence(key: str, headers: Mapping,
                            body: Mapping) -> str:
    """Name the wire slots that carried *key*, plus a short key digest.

    The cache-break detector compares this value between rounds, so a lineage
    that changed its key or lost its carrier shows up as a routing change. The
    per-round record keeps it, so reports can split cache reuse by carrier.
    Returns '' when the request carried no lineage key.
    """
    if not key:
        return ''
    slots = sorted(str(name) for name, value in headers.items()
                   if value == key)
    if body.get('prompt_cache_key') == key:
        slots.append('prompt_cache_key')
    if not slots:
        return ''
    short = hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]
    return f'{",".join(slots)}@{short}'
