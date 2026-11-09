"""Immutable admitted request at provider ingress; no cache or prompt telemetry.

Admission counts canonical input locally. Provider translation then seals the
wire JSON; HTTP transports send those bytes, never caller aliases. Legacy
mapping consumers can still obtain a fresh disposable envelope.
Two bounded digests distinguish semantic input from provider prefix projection.
They are research evidence, not a claim about a provider's hidden cache key.
The sealed bytes are the only encode of the provider wire: the prefix digest
hashes their field slices, transport diagnostics hash their per-message and
per-field slices, and detachment copies containers, not JSON text.
Settled (deep-immutable) messages are shared instead of detached and keep
their encodings in their own memo slot, so a later request over the same
settled history encodes and hashes only its new messages.
"""
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import threading

from types import MappingProxyType

from lib.conversation_sync.settled_rows import (
    SettledDict, SettledList, is_settled, memo_get, memo_put,
)
from lib.utils import sha256_holding_gil, update_holding_gil

_PROVIDER_PREFIX_FIELDS = frozenset({
    'model', 'messages', 'input', 'system', 'instructions', 'tools',
    'prompt_cache_key'})
# Exact immutable JSON leaves are their own detached copy; floats must be finite.
_SHARED_JSON_TYPES = frozenset({str, int, bool, type(None)})
# ``json.dumps`` with these options builds the same encoder on every call.
# ``JSONEncoder.encode`` keeps its circular-reference state per call, so the
# shared instances hold configuration only and serve concurrent requests.
_WIRE_ENCODER = json.JSONEncoder(
    ensure_ascii=False, separators=(',', ':'), allow_nan=False)
_SORTED_ENCODER = json.JSONEncoder(
    ensure_ascii=False, separators=(',', ':'), allow_nan=False, sort_keys=True)
# Wire fields whose items (messages / Responses input) are encoded one by one.
_ITEM_LIST_FIELDS = frozenset({'messages', 'input'})
_JSON_LISTS = (list, SettledList)
# Memo keys on settled rows; values are pure functions of the immutable row.
_EXACT_JSON_MEMO = ('request-json', 'exact')
_SEMANTIC_CHAIN_MEMO = ('request-json', 'semantic-chain')
_WIRE_ITEM_MEMO = ('request-json', 'wire-item')
_SEMANTIC_FIELDS = ('model', 'messages', 'system', 'tools')


def _encode(value, *, sorted_keys=False):
    try:
        encoder = _SORTED_ENCODER if sorted_keys else _WIRE_ENCODER
        return encoder.encode(value).encode('utf-8')
    except (ValueError, TypeError, RecursionError):
        # JSON/UTF-8 errors can contain caller data. They are deterministic,
        # unsent failures, not evidence against a provider or credential.
        from lib.llm.llm_errors import LocalRequestPreparationError
        raise LocalRequestPreparationError(
            'request_json_invalid', stage='request_serialization') from None


def require_wire_protocol(protocol):
    """Reject a misbound route before silently sending a different protocol."""
    if protocol is None or protocol == '':
        return 'openai'
    if protocol not in ('openai', 'anthropic', 'responses'):
        from lib.llm.llm_errors import LocalRequestPreparationError
        raise LocalRequestPreparationError(
            'unsupported_wire_protocol', stage='wire_projection')
    return protocol


@contextmanager
def request_preparation(*, stage='wire_projection'):
    """One unsent-failure boundary for sync, async and non-stream transports.

    Preserve actionable admission/cancellation types. Unexpected local bugs
    retain their cause for diagnostics and credential-source outage routing,
    without copying arbitrary request fragments into the surfaced message.
    This scope must end before provider I/O or budget dispatch is attempted.
    """
    from lib.llm.llm_errors import (
        AbortedError, InvalidImageError, LocalRequestPreparationError, PromptTooLongError,
    )
    try:
        yield
    except (LocalRequestPreparationError, AbortedError, PromptTooLongError, InvalidImageError):
        raise
    except Exception as error:
        raise LocalRequestPreparationError(
            'local request preparation failed', stage=stage) from error


def _digest(value, *, sorted_keys=False):
    return sha256_holding_gil(
        _encode(value, sorted_keys=sorted_keys)).hexdigest()


def _detached_json(value):
    """``json.loads(_encode(value))`` without serializing exact JSON values.

    Every list and dict is fresh; immutable exact leaves are shared. Other
    carriers (tuples, subclasses, non-string keys, non-finite floats, foreign
    objects) take that encode/decode round trip, including its error.
    """
    kind = type(value)
    if kind is list:
        return [item if type(item) in _SHARED_JSON_TYPES else _detached_json(item)
                for item in value]
    if kind is dict:
        detached = {}
        for key, item in value.items():
            if type(key) is not str:
                return json.loads(_encode(value))
            detached[key] = (item if type(item) in _SHARED_JSON_TYPES
                             else _detached_json(item))
        return detached
    if kind in _SHARED_JSON_TYPES or (kind is float and math.isfinite(value)):
        return value
    if (kind is SettledDict or kind is SettledList) and _exact_settled_json(value):
        # Immutable and equal to its JSON round trip: nothing to detach.
        return value
    return json.loads(_encode(value))


def _exact_settled_json(row):
    """Whether a settled tree equals its JSON round trip; memoized on the row."""
    exact = memo_get(row, _EXACT_JSON_MEMO)
    if exact is None:
        try:
            exact = _is_exact_json(row)
        except RecursionError:
            return False
        memo_put(row, _EXACT_JSON_MEMO, exact)
    return exact


def _is_exact_json(value):
    kind = type(value)
    if kind is SettledDict:
        return all(type(key) is str and _is_exact_json(item)
                   for key, item in value.items())
    if kind is SettledList:
        return all(map(_is_exact_json, value))
    return kind in _SHARED_JSON_TYPES or (kind is float and math.isfinite(value))


def detach_input(body):
    """Detach count-bearing JSON before counting and subsequent translation.

    The result equals a JSON round trip of each field without encoding it.
    Settled rows equal to their round trip are shared: they cannot change.
    Shared strings and integers are validated by the semantic identity's
    encode, which callers run before counting.
    """
    for key in ('messages', 'system', 'tools'):
        if key in body:
            try:
                body[key] = _detached_json(body[key])
            except RecursionError:
                # Cycles and nesting beyond Python's frame limit keep the
                # reference outcome: a deep decode or the unsent JSON error.
                body[key] = json.loads(_encode(body[key]))


# Digest state after ``{"messages":[``; only ever copied, never updated.
_SEMANTIC_HEAD = sha256_holding_gil(b'{"messages":[')


def semantic_request_id(body):
    """Content identity, independent of endpoint and generation parameters.

    SHA-256 of the sorted-key compact JSON of model/messages/system/tools.
    ``messages`` sorts first, so with settled messages the digest advances
    message by message: a settled message keeps the digest state after itself
    together with the state it extended, and a later request over the same
    settled prefix resumes there, encoding and hashing only what follows.
    """
    messages = body.get('messages')
    if not (type(messages) in _JSON_LISTS and any(map(is_settled, messages))):
        return _digest({key: body.get(key) for key in _SEMANTIC_FIELDS},
                       sorted_keys=True)
    state = _SEMANTIC_HEAD
    for message in messages:
        settled = is_settled(message)
        if settled:
            link = memo_get(message, _SEMANTIC_CHAIN_MEMO)
            if link is not None and link[0] is state:
                state = link[1]
                continue
        extended = state.copy()
        if state is not _SEMANTIC_HEAD:
            update_holding_gil(extended, b',')
        update_holding_gil(extended, _encode(message, sorted_keys=True))
        if settled:
            memo_put(message, _SEMANTIC_CHAIN_MEMO, (state, extended))
        state = extended
    rest = _encode({key: body.get(key) for key in _SEMANTIC_FIELDS
                    if key != 'messages'}, sorted_keys=True)
    digest = state.copy()
    update_holding_gil(digest, b'],' + rest[1:])
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class AdmittedRequest(Mapping):
    """One attempt's sole retained wire snapshot and content-bound evidence.

    Mapping reads return detached values for legacy plan consumers. No mutable
    prompt reference or caller-supplied token evidence can authorize this value.
    The snapshot expires with its request; only fixed-size IDs enter diagnostics.
    """
    payload: bytes = field(repr=False)
    model: str
    provider_id: str
    protocol: str
    endpoint: str
    input_tokens: int
    semantic_id: str
    provider_prefix_id: str

    _fields: Mapping = field(repr=False, compare=False)
    logical_model: str = ''
    # HTTP Content-Encoding applied to ``payload`` only at the byte transport
    # boundary; JSON readers and WebSocket frames always see plain JSON.
    content_encoding: str = ''
    # Per item of ``messages``/``input``: (start, end, fields) offsets of its
    # sealed bytes; fields are ((name, start, end), ...) relative to the item,
    # or None for an item that is not a string-keyed object.
    _items: Mapping = field(default=MappingProxyType({}), repr=False, compare=False)
    _context_lineage: bytes = field(default=b'', repr=False, compare=False)

    def sealed_items(self, key):
        """Offsets of each sealed ``key`` item and its top-level fields."""
        return self._items.get(key)

    def wire_body(self):
        return json.loads(self.payload)

    def encoded_payload(self):
        if self.content_encoding:
            from lib.llm.request_encoding import zstd_compress
            return zstd_compress(self.payload)
        return self.payload

    def __getitem__(self, key):
        start, end, scalar, value = self._fields[key]
        return value if scalar else json.loads(self.payload[start:end])

    def __iter__(self):
        return iter(self._fields)

    def __len__(self):
        return len(self._fields)

    def diagnostics(self):
        return {'semantic_request_id': self.semantic_id,
                'provider_prefix_id': self.provider_prefix_id,
                'admitted_input_tokens': self.input_tokens}

    def context_diagnostics(self):
        """Detached provenance bound to this exact immutable request, off-wire."""
        if not self._context_lineage:
            return None
        return {**json.loads(self._context_lineage),
                'requestIdentity': self.diagnostics()}

    def usage_evidence(self, trace_id):
        """The same sealed request identity and lineage for every transport."""
        evidence = {'_request_identity': self.diagnostics()}
        lineage = self.context_diagnostics()
        if lineage:
            evidence['_wire_context_lineage'] = {**lineage, 'traceId': trace_id}
        return evidence


def strip_internal_request_fields(body):
    """Project a request/message envelope without Tofu's execution metadata.

    Shared by admitted execution and bounded diagnostic probes. This does not
    grant admission or change nested tool arguments and user content.
    """
    if any(not isinstance(key, str) for key in body):
        from lib.llm.llm_errors import LocalRequestPreparationError
        raise LocalRequestPreparationError(
            'request_field_name_invalid', stage='request_serialization')
    return {key: value for key, value in body.items() if not key.startswith('_')}


class ModelRequestBudgetScope:
    """Required per-attempt budget settlement through an injected local port.

    Transports own the attempt outcome. The execution owner supplies policy;
    private callbacks never enter sealed provider JSON. Nested bridge scopes
    delegate ownership of attempts so caller cancellation cannot settle them early.
    """
    def __init__(self, body):
        self.body = body
        self.leases = []
        self.usage = None
        self.verified_complete = True
        self._lock = threading.Lock()
        self._closed = False

    def __enter__(self):
        admission = self.body.get('_model_budget_admit')
        if callable(admission):
            # A delegated transport owns its physical attempts until it exits,
            # including after its async caller is cancelled.
            admission = getattr(admission, '_model_budget_origin', admission)
            self.body = dict(self.body)
            def admit(**counts):
                lease = admission(**counts)
                with self._lock:
                    if self._closed:
                        lease.settle(unsent=True)
                        from lib.llm.llm_errors import AbortedError
                        raise AbortedError('Model request scope closed before dispatch')
                    self.leases.append(lease)
                return lease
            admit._model_budget_origin = admission
            self.body['_model_budget_admit'] = admit
        return self

    def __exit__(self, kind, error, traceback):
        with self._lock:
            self._closed = True
            leases = tuple(self.leases)
        usage = self.usage if error is None else getattr(error, 'usage', None)
        from lib.llm.llm_errors import provider_refused_request
        refused = usage is None and provider_refused_request(error)
        for lease in leases:
            if refused:
                note = getattr(lease, 'note_provider_refusal', None)
                if callable(note):
                    note()
            lease.settle(usage, unsent=not lease.dispatched and usage is None)
            confirm = getattr(lease, 'confirm_input', None)
            if confirm is not None:
                confirm(successful=error is None and self.verified_complete and lease is leases[-1])


def mark_model_request_dispatched(plan):
    """Required budget evidence just before the transport attempts a send."""
    lease = getattr(plan, 'model_budget_lease', None)
    if lease is not None:
        prepare = getattr(lease, 'prepare_input', None)
        if prepare is not None:
            prepare(getattr(plan, 'body', {}))
        lease.mark_dispatched()


def dispatch_model_request(plan, send, *args, **kwargs):
    """Arguments are prepared before marking an ambiguous physical attempt."""
    mark_model_request_dispatched(plan)
    return send(*args, **kwargs)


def seal_request(body, *, model, provider_id='', protocol='openai',
                 endpoint='', input_tokens, semantic_id, logical_model='',
                 content_encoding=''):
    """Seal a translated body; admission evidence is supplied by ingress only."""
    wire = strip_internal_request_fields(body)
    # Encode each field once (message lists item by item, so diagnostics hash
    # the same bytes); offsets retain no second image/prompt copy.
    chunks = [b'{']
    offsets = {}
    items = {}
    prefix_fields = []
    position = 1
    for key, value in wire.items():
        encoded_key = _encode(key)
        header = (b',' if offsets else b'') + encoded_key + b':'
        chunks.append(header)
        start = position + len(header)
        if key in _ITEM_LIST_FIELDS and type(value) in _JSON_LISTS:
            position, items[key] = _append_items(chunks, value, start)
        else:
            encoded = _encode(value)
            chunks.append(encoded)
            position = start + len(encoded)
        scalar = value is None or isinstance(value, (str, int, float, bool))
        offsets[key] = (start, position, scalar, value if scalar else None)
        if key in _PROVIDER_PREFIX_FIELDS:
            prefix_fields.append((encoded_key, start, position))
    chunks.append(b'}')
    payload = b''.join(chunks)
    prefix_id = _provider_prefix_digest(
        payload, prefix_fields, (model, provider_id, protocol, endpoint))
    return AdmittedRequest(payload, model, provider_id, protocol, endpoint,
                           input_tokens, semantic_id, prefix_id,
                           MappingProxyType(offsets), logical_model or model,
                           content_encoding=content_encoding,
                           _items=MappingProxyType(items),
                           _context_lineage=_seal_context_lineage(body, wire, payload))


def _seal_context_lineage(body, wire, payload):
    if not body.get('_context_lineage'):
        return b''
    from copy import deepcopy
    from lib.llm_sanitize.lineage import verify_lineage
    trace = deepcopy(body['_context_lineage'])
    verify_lineage(trace, wire, 'request_sealing')
    # Archives normalize JSON key order. Keep an exact transport digest AND
    # an order-independent artifact digest; never call the latter wire bytes.
    return _encode({**trace,
                    'wireBodyHash': hashlib.sha256(payload).hexdigest(),
                    'requestContentHash': _digest(wire, sorted_keys=True)})


def _append_items(chunks, values, start):
    """Append ``_encode(values)`` item by item; return its end and item spans."""
    chunks.append(b'[')
    position = start + 1
    spans = []
    for index, value in enumerate(values):
        if index:
            chunks.append(b',')
            position += 1
        encoded, fields = _encoded_item(value)
        chunks.append(encoded)
        spans.append((position, position + len(encoded), fields))
        position += len(encoded)
    chunks.append(b']')
    return position + 1, tuple(spans)


def _encoded_item(value):
    """``_encode(value)`` of one message/input item and its fields' offsets.

    A dict with string keys is encoded field by field: compact JSON frames
    each member as ``"key":value``, so the joined pieces equal the whole
    encode and every field's bytes are addressable. Settled items memoize the
    result in their own memo slot.
    """
    settled = is_settled(value)
    if settled:
        cached = memo_get(value, _WIRE_ITEM_MEMO)
        if cached is not None:
            return cached
    if type(value) in (dict, SettledDict) and all(type(key) is str for key in value):
        parts = [b'{']
        fields = []
        position = 1
        for key, item in value.items():
            header = (b',' if fields else b'') + _encode(key) + b':'
            encoded = _encode(item)
            start = position + len(header)
            position = start + len(encoded)
            fields.append((key, start, position))
            parts += (header, encoded)
        parts.append(b'}')
        result = (b''.join(parts), tuple(fields))
    else:
        result = (_encode(value), None)
    if settled:
        memo_put(value, _WIRE_ITEM_MEMO, result)
    return result


def _provider_prefix_digest(payload, prefix_fields, route):
    """SHA-256 of ``_encode([*route, ordered_prefix_fields])``, from sealed bytes.

    Hash the actual ordered provider-visible prefix, not sampling/output knobs.
    Compact JSON frames array items and object members around each value's
    standalone encoding, so the sealed field slices are that second encode.
    """
    hasher = hashlib.sha256()
    update_holding_gil(
        hasher, b'[' + b','.join(_encode(part) for part in route) + b',{')
    sealed = memoryview(payload)
    for index, (encoded_key, start, end) in enumerate(prefix_fields):
        update_holding_gil(hasher, (b',' if index else b'') + encoded_key + b':')
        update_holding_gil(hasher, sealed[start:end])
    update_holding_gil(hasher, b'}]')
    return hasher.hexdigest()


def recount_anthropic_input(body, *, model, provider_id='', text_count_view=None):
    """Project final translated/cloaked input into the local counter vocabulary.

    Native tool use and images must not disappear during this last admission;
    opaque thinking signatures remain excluded, as in canonical admission.
    ``text_count_view`` is the canonical body's provider view; the projection
    counts through its own sibling view of the same owner memo.
    """
    from lib.llm.body._admission import enforce_body_input_limit
    from lib.token_counter import request_text_count_view
    from lib.token_counter.evidence import TEXT_COUNT_VIEW_KEY

    messages = []
    for message in body.get('messages') or ():
        projected = dict(message)
        content = message.get('content')
        if isinstance(content, list):
            blocks, calls, thinking = [], [], []
            for block in content:
                kind = block.get('type') if isinstance(block, dict) else None
                if kind == 'tool_use':
                    calls.append({'function': {
                        'name': block.get('name', ''),
                        'arguments': _encode(block.get('input', {})).decode()}})
                elif kind == 'image':
                    blocks.append({'type': 'image_url'})
                elif kind == 'thinking':
                    thinking.append({'text': block.get('thinking', '')})
                else:
                    blocks.append(block)
            projected.update(content=blocks, tool_calls=calls,
                             reasoning_content=thinking)
        messages.append(projected)
    canonical = {'messages': messages, 'system': body.get('system'),
                 'tools': body.get('tools')}
    view = request_text_count_view(text_count_view)
    if view is not None:
        canonical[TEXT_COUNT_VIEW_KEY] = view.sibling('provider:anthropic')
    return enforce_body_input_limit(
        canonical, model=model, provider_id=provider_id)


def transport_body(body):
    """Fresh wire envelope at the existing fakeable HTTP/WS transport seam."""
    return body.wire_body() if isinstance(body, AdmittedRequest) else body


def transport_body_arguments(body, *, transport):
    """Use admitted JSON once; ordinary bodies keep their client's JSON path."""
    if transport not in ('requests', 'httpx'):
        raise ValueError('unsupported HTTP transport: ' + str(transport))
    if isinstance(body, AdmittedRequest):
        return {'data' if transport == 'requests' else 'content':
                body.encoded_payload()}
    return {'json': body}


def transport_payload(body):
    """Byte-oriented egress reuses sealed JSON; legacy encoding stays intact."""
    return (body.encoded_payload() if isinstance(body, AdmittedRequest)
            else json.dumps(body).encode())
