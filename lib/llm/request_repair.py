"""lib/llm/request_repair.py — Evidence-driven, restrict-only request repair.

Responsibility: shape canonical (OpenAI-shape) request bodies from typed
provider rejections observed on a route, instead of from model-name rules.
Every repair only REMOVES optional request state; nothing is ever added.

* Route-scoped learned omissions. An ``UnsupportedRequestParameterError``
  names one parameter from the closed ``REPAIRABLE_PARAMETER_WIRE_NAMES``
  vocabulary. The same request is retried once without it and the omission is
  learned for the body's ``_route_output_limit_key`` (provider + wire model
  only when that key is absent). Facts carry provenance, are bounded to
  ``MAX_OMISSION_ROUTES`` routes, expire ``OMISSION_TTL_S`` after the last
  rejection (so a gateway fix is re-learned), and persist in
  ``server_config.json`` under ``route_request_omissions`` with the same
  sanitize/repair discipline as ``lib/model_info/_limits.py``.
* Conversation-scoped thinking withdrawal. A ``ThinkingBlockBindingError``
  (Anthropic preserved-thinking prefix mismatch) withdraws every replayed
  thinking/redacted_thinking block once. sha256 digests of the withdrawn
  signatures — never the signatures — are remembered per conversation in a
  bounded process-local LRU with TTL, and later requests drop exactly the
  Anthropic-signed state of those blocks; plain reasoning stays for other
  protocols and blocks produced after the withdrawal stay.

Entry points:
* ``apply_learned_request_facts`` — pre-send hook, before protocol translation.
* ``repair_rejected_request`` — same-slot repair inside transport retry loops.
* ``RequestRepairState`` — per-request ledger; each repair applies at most once.
* ``mark_status_line_refusal`` — budget witness for a non-200 rejection, so
  the clamp/repair retry that answers it stays admissible.

Dependencies: ``lib.llm.llm_errors`` (typed errors and vocabulary),
``lib.config_dir`` and ``lib.json_store`` (durable facts), stdlib otherwise.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from collections import OrderedDict

from lib.llm.llm_errors import (
    REPAIRABLE_PARAMETER_WIRE_NAMES,
    REQUEST_REPAIR_ERRORS,
    ModelLimitError,
    ThinkingBlockBindingError,
    UnsupportedRequestParameterError,
    _PROVIDER_REFUSAL_ATTR,
    mark_provider_refusal,
)
from lib.log import get_logger

logger = get_logger(__name__)

THINKING_DISPLAY = 'thinking.display'
REPAIRABLE_REQUEST_PARAMETERS = frozenset(REPAIRABLE_PARAMETER_WIRE_NAMES.values())
# Canonical body keys removed for each top-level repairable parameter.
_PARAMETER_BODY_KEYS = {
    'temperature': ('temperature',),
    'top_p': ('top_p',),
    'top_k': ('top_k',),
    'presence_penalty': ('presence_penalty',),
    'frequency_penalty': ('frequency_penalty',),
    'stop': ('stop', 'stop_sequences'),
    'seed': ('seed',),
    'parallel_tool_calls': ('parallel_tool_calls',),
    # ``top_logprobs`` is rejected without ``logprobs``.
    'logprobs': ('logprobs', 'top_logprobs'),
    'reasoning_effort': ('reasoning_effort',),
}

ROUTE_OMISSIONS_CONFIG_KEY = 'route_request_omissions'
OMISSION_TTL_S = 7 * 24 * 3600
MAX_OMISSION_ROUTES = 2_048
_MAX_ROUTE_KEY_CHARS = 80
_MAX_FACT_COUNT = 1_000_000
_MAX_ERROR_CODE_CHARS = 64
_CLOCK_SKEW_S = 300.0

WITHDRAWN_THINKING_TTL_S = 7 * 24 * 3600
MAX_WITHDRAWN_CONVERSATIONS = 1_024
MAX_WITHDRAWN_DIGESTS_PER_CONVERSATION = 512
_MAX_CONVERSATION_ID_CHARS = 256

_THINKING_BLOCK_TYPES = frozenset({'thinking', 'redacted_thinking'})
_THINKING_MESSAGE_FIELDS = ('thinking_signature', 'reasoning_content',
                            'reasoning_details')


class RequestRepairState:
    """Per-request ledger bounding same-slot repairs.

    ``omitted_parameters`` only grows within the closed repairable vocabulary
    and ``thinking_withdrawn`` flips once, so one request makes at most
    ``len(REPAIRABLE_REQUEST_PARAMETERS) + 1`` repaired attempts; both
    transports further cap all same-request restarts at ``MAX_STREAM_RETRIES``.
    The keys are captured by the pre-send hook for callers that rebuild the
    body.
    """

    __slots__ = ('omitted_parameters', 'thinking_withdrawn', 'route_key',
                 'conversation_key')

    def __init__(self):
        self.omitted_parameters: set[str] = set()
        self.thinking_withdrawn = False
        self.route_key = ''
        self.conversation_key = ''

    @property
    def repairs(self) -> int:
        """Repaired attempts this request has already made."""
        return len(self.omitted_parameters) + int(self.thinking_withdrawn)


def mark_status_line_refusal(error):
    """Witness a non-200 rejection that consumed no provider tokens.

    ``mark_provider_refusal`` covers the waitable capacity class. Output-limit,
    unsupported-parameter and thinking-binding rejections are request
    validation answered before inference, and the transports answer them with
    a same-request clamp or repair retry that a finite execution budget must
    still admit. Only a caller holding the non-200 status line may use this:
    an SSE error frame after HTTP 200 proves nothing about spend.
    """
    mark_provider_refusal(error)
    if isinstance(error, (ModelLimitError, *REQUEST_REPAIR_ERRORS)):
        setattr(error, _PROVIDER_REFUSAL_ATTR, True)
    return error


# ── Identities ───────────────────────────────────────────────────────────

def _valid_route_key(route_key) -> bool:
    return (isinstance(route_key, str) and bool(route_key)
            and route_key == route_key.strip()
            and len(route_key) <= _MAX_ROUTE_KEY_CHARS)


def request_fact_route_key(body, *, provider_id: str = '') -> str:
    """Deployment-precise route identity; provider + wire model as fallback."""
    route_key = body.get('_route_output_limit_key')
    if isinstance(route_key, str) and route_key:
        return route_key if _valid_route_key(route_key) else ''
    model = body.get('model')
    if not isinstance(model, str) or not model:
        return ''
    provider = str(body.get('_admission_provider_id') or provider_id or '')
    identity = json.dumps([provider, model], ensure_ascii=False,
                          separators=(',', ':')).encode('utf-8')
    return 'wire:v1:' + hashlib.sha256(identity).hexdigest()


def conversation_key(body) -> str:
    """Conversation identity (``_conv_id``, else ``_task_id``) or ''."""
    for field, prefix in (('_conv_id', 'conv:'), ('_task_id', 'task:')):
        value = body.get(field)
        if (isinstance(value, str) and value
                and len(value) <= _MAX_CONVERSATION_ID_CHARS):
            return prefix + value
    return ''


# ── Route-scoped learned omissions (durable) ─────────────────────────────

def _is_timestamp(value) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _valid_error_code(code) -> str:
    if (isinstance(code, str) and 0 < len(code) <= _MAX_ERROR_CODE_CHARS
            and all(ch.isalnum() or ch in '_.:-' for ch in code)
            and code.isascii()):
        return code
    return ''


def _sanitize_fact(param, fact, now):
    if (param not in REPAIRABLE_REQUEST_PARAMETERS
            or not isinstance(fact, dict) or fact.get('param') != param):
        return None
    first_seen, last_seen = fact.get('first_seen'), fact.get('last_seen')
    if not (_is_timestamp(first_seen) and _is_timestamp(last_seen)):
        return None
    if first_seen > last_seen or last_seen > now + _CLOCK_SKEW_S:
        return None
    if now - last_seen > OMISSION_TTL_S:
        return None
    count = fact.get('count')
    if (not isinstance(count, int) or isinstance(count, bool)
            or not 1 <= count <= _MAX_FACT_COUNT):
        return None
    return {
        'param': param,
        'first_seen': float(first_seen),
        'last_seen': float(last_seen),
        'count': count,
        'error_code': _valid_error_code(fact.get('error_code')),
    }


def _sanitize_route_omissions(raw, *, now, max_routes=MAX_OMISSION_ROUTES):
    """Return ``(bounded unexpired facts, changed)`` for a persisted map.

    Capacity keeps the routes with the newest evidence (``last_seen``). A
    missing map is not a change, so loading never creates the key.
    """
    source = raw if isinstance(raw, dict) else {}
    cleaned: dict[str, dict[str, dict]] = {}
    for route_key, facts in source.items():
        if not _valid_route_key(route_key) or not isinstance(facts, dict):
            continue
        kept = {}
        for param, fact in facts.items():
            valid = _sanitize_fact(param, fact, now)
            if valid is not None:
                kept[param] = valid
        if kept:
            cleaned[route_key] = kept
    cleaned = _keep_newest_routes(cleaned, max_routes)
    changed = raw is not None and (not isinstance(raw, dict) or cleaned != raw)
    return cleaned, changed


def _keep_newest_routes(facts, max_routes=MAX_OMISSION_ROUTES):
    if len(facts) <= max_routes:
        return facts
    newest = sorted(
        facts.items(),
        key=lambda item: max(fact['last_seen'] for fact in item[1].values()))
    return dict(newest[-max_routes:])


def _merge_newest_facts(live, stored):
    """Union two sanitized maps keeping the newest evidence per fact.

    A durable write can return after a later writer already refreshed the
    in-memory map; newest ``last_seen`` (then ``count``) wins, so the map
    never moves backwards whatever order concurrent writes complete in.
    """
    merged = {route_key: dict(facts) for route_key, facts in live.items()}
    for route_key, facts in stored.items():
        route_facts = merged.setdefault(route_key, {})
        for param, fact in facts.items():
            current = route_facts.get(param)
            if current is None or ((fact['last_seen'], fact['count'])
                                   > (current['last_seen'], current['count'])):
                route_facts[param] = fact
    return _keep_newest_routes(merged)


def _next_fact(previous, param, error_code, now):
    if previous is None or now - previous['last_seen'] > OMISSION_TTL_S:
        return {'param': param, 'first_seen': now, 'last_seen': now,
                'count': 1, 'error_code': error_code}
    return {
        'param': param,
        'first_seen': previous['first_seen'],
        'last_seen': max(now, previous['last_seen']),
        'count': min(previous['count'] + 1, _MAX_FACT_COUNT),
        'error_code': error_code or previous['error_code'],
    }


def _repair_persisted_omissions(cfg_path: str, now: float) -> None:
    """Sanitize the file's current map under its cross-writer lock."""
    from lib.json_store import update_json_atomic

    def _mutate(cfg):
        if not isinstance(cfg, dict):
            cfg = {}
        facts, _ = _sanitize_route_omissions(
            cfg.get(ROUTE_OMISSIONS_CONFIG_KEY), now=now)
        cfg[ROUTE_OMISSIONS_CONFIG_KEY] = facts
        return cfg

    update_json_atomic(cfg_path, _mutate, default={})


def _load_route_omissions(now: float) -> dict:
    try:
        from lib.config_dir import config_path
        cfg_path = config_path('server_config.json')
        if not os.path.isfile(cfg_path):
            return {}
        with open(cfg_path, encoding='utf-8') as handle:
            cfg = json.load(handle)
        if not isinstance(cfg, dict):
            return {}
        facts, changed = _sanitize_route_omissions(
            cfg.get(ROUTE_OMISSIONS_CONFIG_KEY), now=now)
        if changed:
            try:
                _repair_persisted_omissions(cfg_path, now)
            except Exception as error:
                # Keep the valid bounded projection already loaded.
                logger.warning(
                    '[RequestRepair] Failed to repair learned request '
                    'omissions: %s', error)
        if facts:
            logger.info('[RequestRepair] Loaded learned request omissions '
                        'for %d routes', len(facts))
        return facts
    except Exception as error:
        logger.warning(
            '[RequestRepair] Failed to load learned request omissions: %s',
            error)
        return {}


class _RouteOmissionStore:
    """Process-wide learned omissions, loaded lazily on first use.

    Writers replace the mapping copy-on-write under ``_lock`` so the per-
    request read path never takes the lock after the first load. The durable
    write runs outside it (the file's own lock orders writers) and its stored
    view merges back newest-wins.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._facts: dict[str, dict[str, dict]] | None = None

    def _snapshot(self, now: float) -> dict:
        facts = self._facts
        if facts is None:
            with self._lock:
                if self._facts is None:
                    self._facts = _load_route_omissions(now)
                facts = self._facts
        return facts

    def omitted_parameters(self, route_key: str, now: float) -> tuple:
        if not route_key:
            return ()
        facts = self._snapshot(now).get(route_key)
        if not facts:
            return ()
        return tuple(sorted(
            param for param, fact in facts.items()
            if now - fact['last_seen'] <= OMISSION_TTL_S))

    def facts_for(self, route_key: str, now: float) -> dict:
        """Detached unexpired provenance for one route (diagnostics/tests)."""
        facts = self._snapshot(now).get(route_key) or {}
        return {param: dict(fact) for param, fact in facts.items()
                if now - fact['last_seen'] <= OMISSION_TTL_S}

    def learn(self, route_key: str, param: str, error_code: str,
              now: float) -> bool:
        """Record one rejection; return True when the fact is new."""
        if (not _valid_route_key(route_key)
                or param not in REPAIRABLE_REQUEST_PARAMETERS):
            return False
        code = _valid_error_code(error_code)
        self._snapshot(now)
        with self._lock:
            facts = dict(self._facts or {})
            route_facts = dict(facts.pop(route_key, {}))
            previous = route_facts.get(param)
            is_new = (previous is None
                      or now - previous['last_seen'] > OMISSION_TTL_S)
            route_facts[param] = _next_fact(previous, param, code, now)
            facts[route_key] = route_facts
            while len(facts) > MAX_OMISSION_ROUTES:
                facts.pop(next(iter(facts)))
            self._facts = facts
        stored = self._persist(route_key, param, code, now)
        if stored is not None:
            with self._lock:
                self._facts = _merge_newest_facts(self._facts or {}, stored)
        if is_new:
            logger.info('[RequestRepair] Learned request omission: param=%s '
                        'route=%s code=%s', param, route_key, code or '-')
        return is_new

    def _persist(self, route_key, param, code, now):
        try:
            from lib.config_dir import config_path
            from lib.json_store import update_json_atomic
            cfg_path = config_path('server_config.json')

            def _mutate(cfg):
                if not isinstance(cfg, dict):
                    cfg = {}
                persisted, _ = _sanitize_route_omissions(
                    cfg.get(ROUTE_OMISSIONS_CONFIG_KEY), now=now)
                route_facts = persisted.pop(route_key, {})
                route_facts[param] = _next_fact(
                    route_facts.get(param), param, code, now)
                persisted[route_key] = route_facts
                persisted, _ = _sanitize_route_omissions(persisted, now=now)
                cfg[ROUTE_OMISSIONS_CONFIG_KEY] = persisted
                return cfg

            stored = update_json_atomic(cfg_path, _mutate, default={})
            return _sanitize_route_omissions(
                (stored or {}).get(ROUTE_OMISSIONS_CONFIG_KEY), now=now)[0]
        except Exception as error:
            logger.error(
                '[RequestRepair] Failed to persist learned request omission: '
                '%s', error, exc_info=True)
            return None


_ROUTE_OMISSIONS = _RouteOmissionStore()


# ── Conversation-scoped thinking withdrawal (process-local) ──────────────

class _WithdrawnThinkingMemory:
    """Bounded LRU of withdrawn block digests per conversation, with TTL.

    Losing an entry (eviction, expiry, restart) is safe: a re-added block
    yields the same typed binding rejection and its one-shot repair.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, tuple[float, dict]] = OrderedDict()

    def remember(self, conversation: str, digests, now: float) -> None:
        if not conversation or not digests:
            return
        with self._lock:
            entry = self._entries.pop(conversation, None)
            known = (entry[1] if entry is not None
                     and now - entry[0] <= WITHDRAWN_THINKING_TTL_S else {})
            for digest in digests:
                known.pop(digest, None)
                known[digest] = None
            while len(known) > MAX_WITHDRAWN_DIGESTS_PER_CONVERSATION:
                known.pop(next(iter(known)))
            self._entries[conversation] = (now, known)
            while len(self._entries) > MAX_WITHDRAWN_CONVERSATIONS:
                self._entries.popitem(last=False)

    def digests(self, conversation: str, now: float) -> frozenset:
        if not conversation or not self._entries:
            return frozenset()
        with self._lock:
            entry = self._entries.get(conversation)
            if entry is None:
                return frozenset()
            if now - entry[0] > WITHDRAWN_THINKING_TTL_S:
                del self._entries[conversation]
                return frozenset()
            self._entries[conversation] = (now, entry[1])
            self._entries.move_to_end(conversation)
            return frozenset(entry[1])


_WITHDRAWN_THINKING = _WithdrawnThinkingMemory()


def _digest(value):
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode('utf-8')).digest()


def _block_digest(block: dict):
    # redacted_thinking carries its opaque state in ``data``, not a signature.
    if block.get('type') == 'redacted_thinking':
        return _digest(block.get('data'))
    return _digest(block.get('signature'))


def _withdraw_message_thinking(message, withdrawn, *, preserve_plain_reasoning=False):
    """Return ``(replacement_or_None, digests)`` for one message.

    ``withdrawn=None`` (the binding repair on the rejecting route) removes all
    thinking state. Remembered digests withdraw only Anthropic-signed state
    bound to them: thinking blocks, the flat ``thinking_signature`` and
    ``reasoning_details`` entries carrying a withdrawn signature, together
    with the ``reasoning_content`` projected into such an entry (a Claude
    gateway rejects it unsigned). Other plain reasoning stays, so a later
    route requiring unsigned reasoning also keeps the text of withdrawn signed
    projections. The selected logical model, not the old block's shape, owns
    that replay requirement.
    """
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return None, []
    if withdrawn is not None:
        return _withdraw_signed_thinking(
            message, withdrawn, preserve_plain_reasoning=preserve_plain_reasoning)
    removed = []
    blocks = message.get('_anthropic_content_blocks')
    blocks_changed = False
    kept_blocks = blocks
    if isinstance(blocks, list):
        kept_blocks = []
        for block in blocks:
            if (isinstance(block, dict)
                    and block.get('type') in _THINKING_BLOCK_TYPES):
                digest = _block_digest(block)
                if digest is not None:
                    removed.append(digest)
                blocks_changed = True
                continue
            kept_blocks.append(block)
    signature_digest = _digest(message.get('thinking_signature'))
    withdraw_fields = any(field in message for field in _THINKING_MESSAGE_FIELDS)
    if not (withdraw_fields or blocks_changed):
        return None, []
    replacement = dict(message)
    if withdraw_fields:
        for field in _THINKING_MESSAGE_FIELDS:
            replacement.pop(field, None)
        if signature_digest is not None:
            removed.append(signature_digest)
        details = message.get('reasoning_details')
        if isinstance(details, list):
            for detail in details:
                if isinstance(detail, dict):
                    digest = _block_digest(detail)
                    if digest is not None:
                        removed.append(digest)
    if blocks_changed:
        if kept_blocks:
            replacement['_anthropic_content_blocks'] = kept_blocks
        else:
            replacement.pop('_anthropic_content_blocks', None)
    return replacement, removed


def _withdraw_signed_thinking(message, withdrawn, *, preserve_plain_reasoning=False):
    removed = []
    replacement = dict(message)
    blocks = message.get('_anthropic_content_blocks')
    if isinstance(blocks, list):
        kept_blocks = []
        for block in blocks:
            if (isinstance(block, dict)
                    and block.get('type') in _THINKING_BLOCK_TYPES):
                digest = _block_digest(block)
                if digest is not None and digest in withdrawn:
                    removed.append(digest)
                    continue
            kept_blocks.append(block)
        if len(kept_blocks) != len(blocks):
            if kept_blocks:
                replacement['_anthropic_content_blocks'] = kept_blocks
            else:
                replacement.pop('_anthropic_content_blocks', None)
    bound = set(withdrawn)
    signature_digest = _digest(message.get('thinking_signature'))
    # The flat signature mirrors this turn's blocks; it is bound with them.
    if signature_digest is not None and (removed or signature_digest in bound):
        replacement.pop('thinking_signature', None)
        removed.append(signature_digest)
        bound.add(signature_digest)
    details = message.get('reasoning_details')
    kept_details = [detail for detail in details
                    if not (isinstance(detail, dict)
                            and _block_digest(detail) in bound)
                    ] if isinstance(details, list) else details
    if isinstance(details, list) and len(kept_details) != len(details):
        if kept_details:
            replacement['reasoning_details'] = kept_details
        else:
            replacement.pop('reasoning_details', None)
        if not preserve_plain_reasoning:
            replacement.pop('reasoning_content', None)
    if replacement == message:
        return None, []
    return replacement, removed


def _withdraw_thinking(body: dict, withdrawn):
    """Copy-on-write ``body['messages']``; return ``(messages_changed, digests)``."""
    messages = body.get('messages')
    if not isinstance(messages, list):
        return 0, []
    preserve_plain_reasoning = False
    if withdrawn is not None:
        from lib.llm.route_features import body_route_profile
        from lib.model_info import model_requires_reasoning_content_replay
        preserve_plain_reasoning = model_requires_reasoning_content_replay(
            body.get('_admission_logical_model') or body.get('model') or '',
            inference_profile=body_route_profile(body))
    replaced = None
    changed = 0
    digests = []
    for index, message in enumerate(messages):
        replacement, removed = _withdraw_message_thinking(
            message, withdrawn, preserve_plain_reasoning=preserve_plain_reasoning)
        if replacement is None:
            continue
        if replaced is None:
            replaced = list(messages)
        replaced[index] = replacement
        changed += 1
        digests.extend(removed)
    if replaced is not None:
        body['messages'] = replaced
    return changed, digests


# ── Body transforms and entry points ─────────────────────────────────────

def omit_request_parameter(body: dict, param: str) -> bool:
    """Remove one repairable parameter in place; True when the body changed."""
    if param == THINKING_DISPLAY:
        thinking = body.get('thinking')
        if not isinstance(thinking, dict) or 'display' not in thinking:
            return False
        body['thinking'] = {key: value for key, value in thinking.items()
                            if key != 'display'}
        return True
    present = [key for key in _PARAMETER_BODY_KEYS.get(param, ())
               if key in body]
    for key in present:
        del body[key]
    return bool(present)


def apply_learned_request_facts(body: dict, *, provider_id: str = '',
                                repair_state: RequestRepairState | None = None,
                                now: float | None = None) -> None:
    """Restrict a canonical body by learned facts before protocol translation.

    Mutates ``body``'s top level in place — callers pass their own copy — and
    replaces (never mutates) nested containers shared with the caller.
    """
    now = time.time() if now is None else now
    route_key = request_fact_route_key(body, provider_id=provider_id)
    conversation = conversation_key(body)
    omitted = set(_ROUTE_OMISSIONS.omitted_parameters(route_key, now))
    if repair_state is not None:
        repair_state.route_key = repair_state.route_key or route_key
        repair_state.conversation_key = (repair_state.conversation_key
                                         or conversation)
        omitted.update(repair_state.omitted_parameters)
    removed = [param for param in sorted(omitted)
               if omit_request_parameter(body, param)]
    if removed:
        logger.debug('[RequestRepair] Omitted learned parameters %s',
                     ','.join(removed))
    if repair_state is not None and repair_state.thinking_withdrawn:
        _withdraw_thinking(body, None)
        return
    withdrawn = _WITHDRAWN_THINKING.digests(conversation, now)
    if withdrawn:
        _withdraw_thinking(body, withdrawn)


def repair_rejected_request(body: dict, error: Exception,
                            state: RequestRepairState, *,
                            provider_id: str = '', log_prefix: str = '',
                            now: float | None = None) -> str:
    """Repair ``body`` in place for one typed rejection; return restart reason.

    Returns '' when the error must propagate: this repair already ran for the
    request, or the canonical body holds nothing the repair could remove.
    """
    now = time.time() if now is None else now
    if isinstance(error, UnsupportedRequestParameterError):
        param = error.param
        if (param not in REPAIRABLE_REQUEST_PARAMETERS
                or param in state.omitted_parameters):
            return ''
        if not omit_request_parameter(body, param):
            return ''
        state.omitted_parameters.add(param)
        _ROUTE_OMISSIONS.learn(
            state.route_key or request_fact_route_key(
                body, provider_id=provider_id),
            param, error.error_code, now)
        logger.warning('%s Retrying without unsupported optional parameter '
                       '%s', log_prefix, param)
        return f'request repair: omit unsupported parameter {param}'
    if isinstance(error, ThinkingBlockBindingError):
        if state.thinking_withdrawn:
            return ''
        changed, digests = _withdraw_thinking(body, None)
        if not changed:
            return ''
        state.thinking_withdrawn = True
        _WITHDRAWN_THINKING.remember(
            state.conversation_key or conversation_key(body), digests, now)
        logger.warning('%s Withdrew thinking history from %d assistant '
                       'message(s) after a binding mismatch — retrying',
                       log_prefix, changed)
        return 'request repair: withdraw bound thinking blocks'
    return ''
