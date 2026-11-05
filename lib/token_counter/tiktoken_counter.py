"""Tier 2 tiktoken backend (OpenAI official tokenizer).

Exact for: GPT-4o, GPT-4, GPT-3.5, o1/o3/o4.
Close-enough for: Qwen, MiniMax, Doubao, GLM, Gemini (BPE with similar
vocabularies — typically within ±10 %).

Encoding choice:
  ``o200k_base`` → GPT-4o, GPT-5, o-series
  ``cl100k_base`` → GPT-4 / 3.5 / everything else
"""

from __future__ import annotations

import json
import re
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from runtime_guards import resolve_resource_budget

from lib.conversation_sync.settled_rows import SettledDict, memo_get, memo_put
from lib.log import get_logger
from lib.utils import sha256_holding_gil

from .base import (
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY,
    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX,
    TokenCounter,
    count_images,
    iter_message_texts,
    _IMAGE_TOKENS_DEFAULT,
    _STRUCTURAL_OVERHEAD_TOKENS,
)
from .heuristic import cheap_estimate_text as _cheap_estimate_text

logger = get_logger(__name__)


_lock = threading.Lock()
_encoders: dict[str, Any] = {}
_available: Optional[bool] = None

# Repeated model rounds count byte-identical tool-schema and compaction blocks.
# Keep only a cryptographic digest and integer exact/heuristic results:
# prompt/schema text never becomes resident cache state. The launch-time
# resource profile bounds the entry count for personal and distributed
# deployments, with a hard ceiling at this consumer boundary.
_TEXT_COUNT_CACHE_MIN_CHARS = 4_096
_TEXT_COUNT_REUSABLE_MIN_CHARS = 512
_TEXT_COUNT_CACHE_CAPACITY = resolve_resource_budget(
    'TOFU_TOKEN_COUNT_CACHE_CAPACITY', maximum=4_096)


@dataclass(frozen=True, slots=True)
class _TextCountCacheEntry:
    """Content-free exact/heuristic counts for one reusable text digest."""

    exact_tokens: int | None = None
    heuristic_tokens: int | None = None


_TextCountCacheKey = tuple[str, int, bytes]
_HEURISTIC_CACHE_SCOPE = 'entropy-heuristic/v1'
# ``encoding_for_model`` has exactly these two outputs. Listing them here lets
# the model-independent entropy estimator reuse whichever tokenizer-scoped
# entry was touched immediately before it, without a second cache or index.
_TOKENIZER_CACHE_SCOPES = ('o200k_base', 'cl100k_base')
_text_count_cache: OrderedDict[
    _TextCountCacheKey, _TextCountCacheEntry
] = OrderedDict()
_text_count_cache_lock = threading.Lock()
_text_count_cache_metrics = {
    'hits': 0,
    'misses': 0,
    'heuristicHits': 0,
    'heuristicMisses': 0,
    'evictions': 0,
}


def _get_encoder(name: str):
    """Return a cached tiktoken encoder, or None if tiktoken isn't installed."""
    from .native import scoped_native_encoder
    native=scoped_native_encoder(name)
    if native is not None:
        return native
    global _available
    if _available is False:
        return None
    with _lock:
        if name in _encoders:
            return _encoders[name]
        try:
            import tiktoken  # type: ignore
            enc = tiktoken.get_encoding(name)
            _encoders[name] = enc
            _available = True
            return enc
        except ImportError:
            logger.info('[TokenCounter] tiktoken not installed — Tier 2 unavailable')
            _available = False
            return None
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken.get_encoding(%s) failed: %s',
                           name, e)
            return None


def encoding_for_model(model: str) -> str:
    """Pick the best tiktoken encoding for a model id."""
    m = (model or '').lower()
    if 'gpt-4o' in m or 'gpt-5' in m or re.search(r'\bo[134]\b', m):
        return 'o200k_base'
    return 'cl100k_base'


def _text_digest(text: str) -> bytes:
    """SHA-256 of the text's code points; equal digests mean equal texts.

    UTF-8 with surrogates passed through encodes every ``str`` injectively.
    Digests are derived for every history text on each count; hashlib would
    hand the GIL away once per text (see lib/utils.py).
    """
    return sha256_holding_gil(
        text.encode('utf-8', errors='surrogatepass')).digest()


def _text_count_cache_key(
    text: str,
    encoding: str,
    *,
    reusable: bool,
    digest: bytes | None = None,
) -> _TextCountCacheKey | None:
    cache_min_chars = (
        _TEXT_COUNT_REUSABLE_MIN_CHARS
        if reusable else _TEXT_COUNT_CACHE_MIN_CHARS
    )
    if len(text) < cache_min_chars:
        return None
    return (
        encoding,
        len(text),
        _text_digest(text) if digest is None else digest,
    )


def _put_text_count_cache_locked(
    cache_key: _TextCountCacheKey,
    entry: _TextCountCacheEntry,
) -> None:
    """Publish one entry while holding ``_text_count_cache_lock``."""
    _text_count_cache[cache_key] = entry
    _text_count_cache.move_to_end(cache_key)
    capacity = max(1, int(_TEXT_COUNT_CACHE_CAPACITY))
    while len(_text_count_cache) > capacity:
        _text_count_cache.popitem(last=False)
        _text_count_cache_metrics['evictions'] += 1


def _heuristic_cache_candidates(
    cache_key: _TextCountCacheKey,
) -> tuple[_TextCountCacheKey, ...]:
    """Return every bounded scope that can carry this model-free estimate."""
    _scope, char_count, digest = cache_key
    return tuple(
        (scope, char_count, digest)
        for scope in (*_TOKENIZER_CACHE_SCOPES, _HEURISTIC_CACHE_SCOPE)
    )


def _find_heuristic_entry_locked(
    cache_key: _TextCountCacheKey,
) -> tuple[_TextCountCacheKey | None, int | None]:
    """Find the shared heuristic value while holding the cache lock."""
    first_existing_key = None
    for candidate in _heuristic_cache_candidates(cache_key):
        entry = _text_count_cache.get(candidate)
        if entry is None:
            continue
        if first_existing_key is None:
            first_existing_key = candidate
        if entry.heuristic_tokens is not None:
            return candidate, entry.heuristic_tokens
    return first_existing_key, None


def _put_exact_count_locked(
    cache_key: _TextCountCacheKey,
    exact_tokens: int,
) -> None:
    """Merge an exact result with any model-independent heuristic entry."""
    existing = _text_count_cache.get(cache_key)
    heuristic_tokens = (
        existing.heuristic_tokens if existing is not None else None)
    if heuristic_tokens is None:
        _heuristic_key, heuristic_tokens = _find_heuristic_entry_locked(
            cache_key)

    # A standalone heuristic scan may have created the neutral carrier first.
    # The tokenizer-scoped exact entry supersedes it, keeping one capacity slot.
    neutral_key = (
        _HEURISTIC_CACHE_SCOPE, cache_key[1], cache_key[2])
    neutral = _text_count_cache.get(neutral_key)
    if (neutral_key != cache_key and neutral is not None
            and neutral.exact_tokens is None):
        _text_count_cache.pop(neutral_key, None)

    _put_text_count_cache_locked(
        cache_key,
        _TextCountCacheEntry(
            exact_tokens=exact_tokens,
            heuristic_tokens=heuristic_tokens,
        ),
    )


def count_text(text: str, model: str = '', *, reusable: bool = False) -> int:
    """Exact-ish count for a single text blob (public API)."""
    if not text:
        return 0
    encoding = encoding_for_model(model)
    cache_key = _text_count_cache_key(
        text, encoding, reusable=reusable)
    if cache_key is not None:
        with _text_count_cache_lock:
            cached = _text_count_cache.get(cache_key)
            if cached is not None and cached.exact_tokens is not None:
                _text_count_cache.move_to_end(cache_key)
                _text_count_cache_metrics['hits'] += 1
                return cached.exact_tokens
            _text_count_cache_metrics['misses'] += 1

    enc = _get_encoder(encoding)
    if enc is None:
        return 0  # caller should fall back to heuristic
    try:
        from .native import NativeTextEncoder
        result = (enc.count_text(text) if isinstance(enc,NativeTextEncoder)
                  else len(enc.encode(text, disallowed_special=())))
    except Exception as e:
        logger.debug('[TokenCounter] tiktoken encode failed: %s', e)
        return 0
    if cache_key is not None:
        with _text_count_cache_lock:
            _put_exact_count_locked(cache_key, result)
    return result


def cached_cheap_estimate_text(
    text: str,
    *,
    reusable: bool = False,
) -> int:
    """Return the entropy estimate, reusing the bounded text-digest cache.

    The estimate is model-independent, so it can share either tokenizer scope.
    Only the digest, length, and integer counts survive the call. ``reusable``
    has the same caller-proof semantics and admission threshold as
    :func:`count_text`. Inside ``RequestTextCountView.estimating()`` a ``str``
    is answered by that owner's memo first; the value never changes.
    """
    if not text:
        return 0
    view = _ACTIVE_ESTIMATE_VIEW.get()
    if view is not None and isinstance(text, str):
        return view.cheap_estimate_text(text, reusable=reusable)
    return _shared_cheap_estimate_text(text, reusable=reusable)


def _shared_cheap_estimate_text(
    text: str,
    *,
    reusable: bool,
    digest: bytes | None = None,
) -> int:
    """``cheap_estimate_text`` through the shared digest cache only."""
    if not text:
        return 0
    neutral_key = _text_count_cache_key(
        text, _HEURISTIC_CACHE_SCOPE, reusable=reusable, digest=digest)
    if neutral_key is None:
        return _cheap_estimate_text(text)

    with _text_count_cache_lock:
        cache_key, cached = _find_heuristic_entry_locked(neutral_key)
        if cached is not None:
            assert cache_key is not None
            _text_count_cache.move_to_end(cache_key)
            _text_count_cache_metrics['heuristicHits'] += 1
            return cached
        _text_count_cache_metrics['heuristicMisses'] += 1

    result = _cheap_estimate_text(text)
    with _text_count_cache_lock:
        # Re-select after computing outside the lock: a concurrent exact count
        # may now offer the better carrier for the same content digest.
        cache_key, raced = _find_heuristic_entry_locked(neutral_key)
        if raced is not None:
            return raced
        target_key = cache_key or neutral_key
        existing = _text_count_cache.get(target_key)
        _put_text_count_cache_locked(
            target_key,
            _TextCountCacheEntry(
                exact_tokens=(
                    existing.exact_tokens if existing is not None else None),
                heuristic_tokens=result,
            ),
        )
    return result


REQUEST_TEXT_VIEW = 'request'
"""Default view: compaction's counts of the request with the task schema."""

# Settled rows are deep-immutable, so a row's per-text results live in its own
# memo slot as long as the row does, and need no digest.
_SETTLED_TEXT_TOKENS_KEY = 'token-counter.text-tokens'
_SETTLED_TEXT_ESTIMATES_KEY = ('token-counter.text-estimates',)

_ACTIVE_ESTIMATE_VIEW: ContextVar['RequestTextCountView | None'] = ContextVar(
    'tofu_request_text_estimate_view', default=None)


@dataclass(frozen=True, slots=True)
class _ToolSchemaText:
    """JSON text and digest of one ordered tuple of held tool-schema objects."""

    schemas: tuple[Any, ...]
    text: str
    digest: bytes

    def matches(self, tools: Any) -> bool:
        schemas = self.schemas
        return (len(schemas) == len(tools)
                and all(held is tool for held, tool in zip(schemas, tools)))


@dataclass(frozen=True, slots=True)
class _ViewSnapshot:
    """One view's latest counted request: digest -> (exact, entropy) counts.

    Exact counts belong to ``encoding``; entropy estimates are model-free.
    """

    encoding: str
    entries: dict[bytes, tuple[int | None, int | None]]
    tool_schema_text: _ToolSchemaText | None = None


_EMPTY_VIEW = _ViewSnapshot('', {})


class RequestTextCounts:
    """Content-free per-text counts of one owner's latest counted requests.

    An agent loop counts the same append-mostly history several times per
    round (compaction and its gate, prompt admission, selected-slot admission,
    stream ingress, an Anthropic recount). Most of those texts are shorter than
    the shared digest cache's admission floor, and a long history can exceed
    that shared LRU, so without this memo every count re-tokenizes and
    re-estimates the whole transcript. Each tokenizer call releases the GIL;
    under a loaded server every re-acquisition can wait a full switch
    interval, turning one round's ~1k calls into seconds of wall time.

    Keys are the SHA-256 digest of each text's code points (UTF-8 with
    surrogates passed through, an injective encoding), computed while holding
    the GIL: a hit means an equal text, and no text is retained. A message's
    count is the sum of its texts' counts, so per-text keys also reuse the
    unchanged texts of an edited or re-projected message. Counts are kept per
    view, one per projection of the request (see :meth:`view`); a view's count
    replaces only that view's entries with exactly its request's texts, while
    every view reads all of them, so alternating projections never evict each
    other, the memo never outgrows one request per view, and texts compaction
    removed drop out. The owner's lifetime bounds it; no capacity knob.
    """

    __slots__ = ('_views', '_round_ledger')

    def __init__(self) -> None:
        # Copy-on-write: each publication assigns a new dict whose snapshots
        # are never mutated afterwards, so concurrent counts on one owner never
        # pair one encoding's counts with another's label; a racing count only
        # loses reuse.
        self._views: dict[str, _ViewSnapshot] = {}
        self._round_ledger: dict[int, tuple[str, bytes]] | None = None

    @contextmanager
    def shared_text_identities(self):
        """Let every view created meanwhile share one identity ledger.

        One owner round counts the same text objects in compaction, admission
        and the provider body; sharing their digests hashes each object once
        per round. The ledger holds the round's own text objects and leaves
        the memo when the round that created it ends, so no text outlives the
        round here. A scope entered meanwhile (a shallow task copy shares the
        memo) joins that ledger and never restores one on exit.
        """
        if self._round_ledger is not None:
            yield self
            return
        ledger: dict[int, tuple[str, bytes]] = {}
        self._round_ledger = ledger
        try:
            yield self
        finally:
            if self._round_ledger is ledger:
                self._round_ledger = None

    def view(
        self,
        label: str = REQUEST_TEXT_VIEW,
        *,
        reuse_tool_schema_identity: bool = False,
    ) -> 'RequestTextCountView':
        """Return a fresh counting view of one projection of the request.

        ``reuse_tool_schema_identity`` lets the view reuse the JSON text of a
        tool list whose ordered schema objects are the same objects as before.
        Only owners whose schema objects are copy-on-write values may set it;
        provider bodies recount schemas from content.
        """
        return RequestTextCountView(
            self, label, reuse_tool_schema_identity=reuse_tool_schema_identity,
            ledger=self._round_ledger)

    @property
    def encoding(self) -> str:
        return self._views.get(REQUEST_TEXT_VIEW, _EMPTY_VIEW).encoding

    @property
    def counts(self) -> dict[bytes, int]:
        entries = self._views.get(REQUEST_TEXT_VIEW, _EMPTY_VIEW).entries
        return {digest: tokens for digest, (tokens, _estimate) in entries.items()
                if tokens is not None}

    def __copy__(self) -> 'RequestTextCounts':
        return self

    def __deepcopy__(self, _memo: Any) -> 'RequestTextCounts':
        # Request snapshots and copied task dicts share the owner's cache.
        return self


def _dump_tool_schemas(tools: Any) -> str | None:
    """Tool-schema text exactly as ``iter_message_texts`` derives it."""
    try:
        return json.dumps(tools, ensure_ascii=False)
    except (TypeError, ValueError) as e:
        logger.debug('[TokenCounter] tool schema json.dumps failed: %s', e)
        return None


class RequestTextCountView:
    """One projection of an owner memo plus its live text identities.

    ``label`` names the projection (compaction, admission, provider body, its
    Anthropic translation). The identity ledger maps a live text object to its
    digest and holds that object, so an identity is never reused while the
    view lives; a view lives for one count call or one provider body, never
    longer than the request it counts.
    """

    __slots__ = ('memo', 'label', 'reuse_tool_schema_identity', '_ledger',
                 '_estimates', '_tool_schema_text', '_depth', '_snapshot_cache')

    def __init__(self, memo: RequestTextCounts, label: str, *,
                 reuse_tool_schema_identity: bool = False,
                 ledger: dict[int, tuple[str, bytes]] | None = None) -> None:
        self.memo = memo
        self.label = label
        self.reuse_tool_schema_identity = reuse_tool_schema_identity
        self._ledger: dict[int, tuple[str, bytes]] = {} if ledger is None else ledger
        self._estimates: dict[bytes, int] = {}
        self._tool_schema_text: _ToolSchemaText | None = None
        self._depth = 0
        self._snapshot_cache: tuple[dict, tuple[_ViewSnapshot, ...]] | None = None

    def sibling(self, label: str) -> 'RequestTextCountView':
        """Another projection of the same request, sharing text identities."""
        return RequestTextCountView(
            self.memo, label,
            reuse_tool_schema_identity=self.reuse_tool_schema_identity,
            ledger=self._ledger)

    def __copy__(self) -> 'RequestTextCountView':
        return self

    def __deepcopy__(self, _memo: Any) -> 'RequestTextCountView':
        return self

    def _snapshots(self) -> tuple[_ViewSnapshot, ...]:
        views = self.memo._views
        cached = self._snapshot_cache
        # Every publication assigns a new views dict, so its identity is the
        # cache's version.
        if cached is not None and cached[0] is views:
            return cached[1]
        own = views.get(self.label)
        others = tuple(snapshot for label, snapshot in views.items()
                       if label != self.label)
        snapshots = others if own is None else (own, *others)
        self._snapshot_cache = (views, snapshots)
        return snapshots

    def _publish(self, snapshot: _ViewSnapshot) -> None:
        self.memo._views = {**self.memo._views, self.label: snapshot}

    def text_digest(self, text: str) -> bytes:
        held = self._ledger.get(id(text))
        if held is not None and held[0] is text:
            return held[1]
        digest = _text_digest(text)
        self._ledger[id(text)] = (text, digest)
        return digest

    def tool_schema_text(self, tools: Any) -> str | None:
        """The tools text of ``iter_message_texts``; ``None`` if it has none."""
        if not tools:
            return None
        reuse = (self.reuse_tool_schema_identity
                 and isinstance(tools, (list, tuple)))
        if reuse:
            held = self._tool_schema_text
            if held is None or not held.matches(tools):
                held = next((snapshot.tool_schema_text
                             for snapshot in self._snapshots()
                             if snapshot.tool_schema_text is not None
                             and snapshot.tool_schema_text.matches(tools)), None)
            if held is not None:
                self._tool_schema_text = held
                self._ledger[id(held.text)] = (held.text, held.digest)
                return held.text
        text = _dump_tool_schemas(tools)
        if reuse and text is not None:
            self._tool_schema_text = _ToolSchemaText(
                tuple(tools), text, self.text_digest(text))
        return text

    def head_texts(self, system: Any, tools: Any) -> list[str]:
        """System and tool texts, in ``iter_message_texts`` order."""
        texts = list(iter_message_texts(None, system, None))
        tools_text = self.tool_schema_text(tools)
        if tools_text is not None:
            texts.append(tools_text)
        return texts

    @contextmanager
    def estimating(self):
        """Answer ``cached_cheap_estimate_text`` from this view meanwhile.

        Estimates gathered inside the outermost scope are published once at
        its end, so a scope over many leaf estimates is one memo pass.
        """
        token = _ACTIVE_ESTIMATE_VIEW.set(self)
        self._depth += 1
        try:
            yield self
        finally:
            self._depth -= 1
            _ACTIVE_ESTIMATE_VIEW.reset(token)
            if not self._depth:
                self._publish_estimates()

    def cheap_estimate_text(self, text: str, *, reusable: bool = True) -> int:
        """Exactly ``cheap_estimate_text(text)`` for a ``str``."""
        if not text:
            return 0
        digest = self.text_digest(text)
        estimate = self._estimates.get(digest)
        if estimate is not None:
            return estimate
        for snapshot in self._snapshots():
            entry = snapshot.entries.get(digest)
            if entry is not None and entry[1] is not None:
                estimate = entry[1]
                break
        else:
            estimate = _shared_cheap_estimate_text(
                text, reusable=reusable, digest=digest)
        self._estimates[digest] = estimate
        return estimate

    def cheap_estimate(self, messages: list, system: Any = None,
                       tools: Any = None) -> int:
        """Exactly ``heuristic.cheap_estimate(messages, system, tools)``."""
        with self.estimating():
            text_tokens = sum(self.cheap_estimate_text(text)
                              for text in self.head_texts(system, tools))
            for message in messages or ():
                texts = iter_message_texts((message,))
                if type(message) is not SettledDict:
                    text_tokens += sum(self.cheap_estimate_text(text)
                                       for text in texts)
                    continue
                estimates = memo_get(message, _SETTLED_TEXT_ESTIMATES_KEY)
                if estimates is None:
                    estimates = memo_put(
                        message, _SETTLED_TEXT_ESTIMATES_KEY,
                        tuple(self.cheap_estimate_text(text) for text in texts))
                text_tokens += sum(estimates)
        return (text_tokens + count_images(messages) * _IMAGE_TOKENS_DEFAULT
                + _STRUCTURAL_OVERHEAD_TOKENS)

    def _publish_estimates(self) -> None:
        estimates, self._estimates = self._estimates, {}
        if not estimates:
            return
        own = self.memo._views.get(self.label, _EMPTY_VIEW)
        # Keep the latest exact request and this pass's texts: bounded by two
        # requests however many estimate-only passes run between counts.
        entries = {digest: entry for digest, entry in own.entries.items()
                   if entry[0] is not None}
        for digest, estimate in estimates.items():
            entry = entries.get(digest)
            entries[digest] = (None if entry is None else entry[0], estimate)
        self._publish(_ViewSnapshot(
            own.encoding, entries,
            self._tool_schema_text or own.tool_schema_text))

    def _publish_counts(
        self,
        encoding: str,
        entries: dict[bytes, tuple[int | None, int | None]],
    ) -> None:
        """Publish this view's request: exact counts with known estimates."""
        gathered = self._estimates
        for digest, estimate in gathered.items():
            entry = entries.get(digest)
            if entry is not None:
                entries[digest] = (entry[0], estimate)
        self._publish(_ViewSnapshot(encoding, entries, self._tool_schema_text))


def request_text_count_view(value: Any) -> RequestTextCountView | None:
    """A genuine view (or a memo's default view); anything else is ``None``."""
    if isinstance(value, RequestTextCountView):
        return value
    if isinstance(value, RequestTextCounts):
        return value.view()
    return None


# tiktoken encodes a special token only when it is explicitly allowed, and it
# cuts the text at that token before regex pre-tokenization. A batch joined
# with ``<|endoftext|>`` therefore encodes every text exactly as
# ``encode(text, disallowed_special=())`` would, between separator tokens.
_BATCH_SEPARATOR = '<|endoftext|>'


def _encode_batch_counts(encoder: Any, texts: list[str]) -> list[int]:
    """Exact per-text token counts from one tokenizer call.

    ``Encoding.encode_batch`` starts an eight-thread pool per call and every
    ``encode`` releases the GIL, so with other threads runnable each text
    waited about a switch interval: ~400 short history texts took 0.8 s
    instead of 8 ms. One call over the separator-joined batch releases the
    GIL once. A text that spells the separator keeps its own call, where the
    separator stays ordinary text. Encoders without that special token keep
    ``encode_batch``; the borrowed native encoder counts its batch natively.
    """
    from .native import NativeTextEncoder
    if isinstance(encoder, NativeTextEncoder):
        return encoder.count_batch(texts)
    separator_token = getattr(encoder, 'eot_token', None)
    if (separator_token is None
            or _BATCH_SEPARATOR not in getattr(encoder, 'special_tokens_set', ())):
        return [len(tokens) for tokens in encoder.encode_batch(
            texts, disallowed_special=())]
    counts = [0] * len(texts)
    joined = [index for index, text in enumerate(texts)
              if _BATCH_SEPARATOR not in text]
    if joined:
        tokens = encoder.encode(
            _BATCH_SEPARATOR.join(texts[index] for index in joined),
            allowed_special={_BATCH_SEPARATOR}, disallowed_special=())
        start = 0
        for index in joined[:-1]:
            end = tokens.index(separator_token, start)
            counts[index] = end - start
            start = end + 1
        counts[joined[-1]] = len(tokens) - start
    for index, text in enumerate(texts):
        if _BATCH_SEPARATOR in text:
            counts[index] = len(encoder.encode(text, disallowed_special=()))
    return counts


def _count_request_texts_with_cache(
    texts: list[str],
    *,
    model: str,
    encoder: Any,
    selected_text_token_counts: dict[int, int | None] | None = None,
    request_memo: RequestTextCounts | RequestTextCountView | None = None,
) -> int:
    """Count one repeated request, batching short text and unique misses.

    Request history proves that its large prefix is reusable across model
    rounds. SHA-256 keys retain no prompt text after this call. Cache lookup and
    fill take the existing bounded lock; hashing and tokenizer work do not.
    """
    encoding = encoding_for_model(model)
    view = request_text_count_view(request_memo)
    if view is not None:
        return _count_texts_with_view(
            texts,
            encoding=encoding,
            encoder=encoder,
            selected_text_token_counts=selected_text_token_counts,
            view=view,
        )[0]
    keyed_texts = [
        (
            text,
            _text_count_cache_key(text, encoding, reusable=True),
        )
        for text in texts
    ]
    short_texts: list[str] = []
    pending: OrderedDict[
        _TextCountCacheKey, tuple[str, int, list[int]]
    ] = OrderedDict()
    total = 0
    with _text_count_cache_lock:
        for text, cache_key in keyed_texts:
            text_identity = id(text)
            selected_identity = (
                text_identity
                if (selected_text_token_counts is not None
                    and text_identity in selected_text_token_counts)
                else None
            )
            if cache_key is None:
                short_texts.append(text)
                continue
            cached = _text_count_cache.get(cache_key)
            if cached is not None and cached.exact_tokens is not None:
                _text_count_cache.move_to_end(cache_key)
                _text_count_cache_metrics['hits'] += 1
                total += cached.exact_tokens
                if selected_identity is not None:
                    selected_text_token_counts[selected_identity] = (
                        cached.exact_tokens)
                continue
            duplicate = pending.get(cache_key)
            if duplicate is not None:
                selected_identities = duplicate[2]
                if selected_identity is not None:
                    selected_identities.append(selected_identity)
                pending[cache_key] = (
                    duplicate[0], duplicate[1] + 1, selected_identities)
                _text_count_cache_metrics['hits'] += 1
                continue
            pending[cache_key] = (
                text,
                1,
                [selected_identity] if selected_identity is not None else [],
            )
            _text_count_cache_metrics['misses'] += 1

    batch_texts = [
        *short_texts,
        *(text for text, _count, _selected in pending.values()),
    ]
    if not batch_texts:
        return total
    counts = _encode_batch_counts(encoder, batch_texts)
    short_count = len(short_texts)
    for text, count in zip(short_texts, counts[:short_count]):
        total += count
        text_identity = id(text)
        if (selected_text_token_counts is not None
                and text_identity in selected_text_token_counts):
            selected_text_token_counts[text_identity] = count

    pending_counts: list[tuple[_TextCountCacheKey, int]] = []
    for (cache_key, (_text, occurrences, selected_identities)), count in zip(
        pending.items(),
        counts[short_count:],
    ):
        total += count * occurrences
        if selected_text_token_counts is not None:
            for text_identity in selected_identities:
                selected_text_token_counts[text_identity] = count
        pending_counts.append((cache_key, count))

    if pending_counts:
        with _text_count_cache_lock:
            for cache_key, count in pending_counts:
                _put_exact_count_locked(cache_key, count)
    return total


def _count_texts_with_view(
    texts: list[str],
    *,
    encoding: str,
    encoder: Any,
    selected_text_token_counts: dict[int, int | None] | None,
    view: RequestTextCountView,
) -> tuple[int, dict[bytes, tuple[int | None, int | None]]]:
    """Count texts, tokenizing only those no view of the owner has counted.

    Returns the total and the published ``digest -> (exact, estimate)``
    entries; publishes this view's latest request only after the tokenizer
    succeeded.
    """
    remembered = [snapshot.entries for snapshot in view._snapshots()
                  if snapshot.encoding == encoding]
    digests = [view.text_digest(text) for text in texts]
    entries: dict[bytes, tuple[int | None, int | None]] = {}
    pending: dict[bytes, str] = {}
    for text, digest in zip(texts, digests):
        if digest in entries or digest in pending:
            continue
        for known in remembered:
            entry = known.get(digest)
            if entry is not None and entry[0] is not None:
                entries[digest] = entry
                break
        else:
            pending[digest] = text
    if pending:
        for digest, count in _count_new_request_texts(
                pending, encoding=encoding, encoder=encoder).items():
            entries[digest] = (count, None)

    total = 0
    for text, digest in zip(texts, digests):
        count = entries[digest][0]
        total += count
        if (selected_text_token_counts is not None
                and id(text) in selected_text_token_counts):
            selected_text_token_counts[id(text)] = count
    view._publish_counts(encoding, entries)
    return total, entries


def _count_request_with_view(
    messages: list,
    system: Any,
    tools: Any,
    *,
    encoding: str,
    encoder: Any,
    selected_text_token_counts: dict[int, int | None] | None,
    view: RequestTextCountView,
) -> int:
    """Text tokens of one request: settled rows from their slots, the rest
    from the owner memo, new texts in one tokenizer call.

    Every text is encoded on its own (``_encode_batch_counts`` equals one
    ``encode`` per text), so the request's text tokens are the sum of its
    texts' tokens: memoized texts plus the new tail plus system/tool parts.
    """
    texts = view.head_texts(system, tools)
    settled_tokens = 0
    unslotted: list[tuple[SettledDict, list[str]]] = []
    slot_key = (_SETTLED_TEXT_TOKENS_KEY, encoding)
    for message in messages or ():
        message_texts = list(iter_message_texts((message,)))
        if type(message) is SettledDict:
            slotted = memo_get(message, slot_key)
            if slotted is not None:
                settled_tokens += sum(slotted)
                if selected_text_token_counts is not None:
                    for text, count in zip(message_texts, slotted):
                        if id(text) in selected_text_token_counts:
                            selected_text_token_counts[id(text)] = count
                continue
            unslotted.append((message, message_texts))
        texts.extend(message_texts)
    if not texts:
        return settled_tokens
    total, counts = _count_texts_with_view(
        texts, encoding=encoding, encoder=encoder,
        selected_text_token_counts=selected_text_token_counts, view=view)
    for message, message_texts in unslotted:
        memo_put(message, slot_key, tuple(
            counts[view.text_digest(text)][0] for text in message_texts))
    return settled_tokens + total


def _count_new_request_texts(
    pending: dict[bytes, str],
    *,
    encoding: str,
    encoder: Any,
) -> dict[bytes, int]:
    """Exact counts for memo misses: shared digest cache, then one encode."""
    shared_keys: dict[bytes, _TextCountCacheKey] = {}
    for digest, text in pending.items():
        shared_key = _text_count_cache_key(
            text, encoding, reusable=True, digest=digest)
        if shared_key is not None:
            shared_keys[digest] = shared_key
    counts: dict[bytes, int] = {}
    if shared_keys:
        with _text_count_cache_lock:
            for digest, shared_key in shared_keys.items():
                cached = _text_count_cache.get(shared_key)
                if cached is not None and cached.exact_tokens is not None:
                    _text_count_cache.move_to_end(shared_key)
                    _text_count_cache_metrics['hits'] += 1
                    counts[digest] = cached.exact_tokens
                else:
                    _text_count_cache_metrics['misses'] += 1
    misses = [digest for digest in pending if digest not in counts]
    if not misses:
        return counts
    encoded = _encode_batch_counts(
        encoder, [pending[digest] for digest in misses])
    counts.update(zip(misses, encoded))
    published = [(shared_keys[digest], count)
                 for digest, count in zip(misses, encoded)
                 if digest in shared_keys]
    if published:
        with _text_count_cache_lock:
            for shared_key, count in published:
                _put_exact_count_locked(shared_key, count)
    return counts


def text_count_cache_snapshot() -> dict[str, int]:
    """Return bounded, content-free cache telemetry for diagnostics/tests."""
    with _text_count_cache_lock:
        return {
            **_text_count_cache_metrics,
            'entries': len(_text_count_cache),
            'capacity': max(1, int(_TEXT_COUNT_CACHE_CAPACITY)),
            'minChars': max(0, int(_TEXT_COUNT_CACHE_MIN_CHARS)),
            'reusableMinChars': max(0, int(_TEXT_COUNT_REUSABLE_MIN_CHARS)),
        }


def _reset_text_count_cache_for_tests() -> None:
    with _text_count_cache_lock:
        _text_count_cache.clear()
        for key in _text_count_cache_metrics:
            _text_count_cache_metrics[key] = 0


class TiktokenCounter(TokenCounter):
    """Local universal tokenizer (OpenAI-exact, rest ±10 %)."""

    name = 'tiktoken'
    # ``confidence`` is refined per-model in ``count()``.
    confidence = 'good'
    needs_network = False

    def supports(self, model: str) -> bool:
        # Always supported as long as tiktoken is installed.
        return _get_encoder('cl100k_base') is not None

    def count(self, messages: list, *, model: str,
              system: Any = None, tools: Any = None,
              **kwargs) -> Optional[int]:
        encoding = encoding_for_model(model)
        enc = _get_encoder(encoding)
        if enc is None:
            return None
        try:
            total = 0
            per_msg_overhead = 4  # role token + separators (OpenAI chat format)
            view = request_text_count_view(kwargs.get('text_count_memo'))
            texts = (None if view is not None
                     else list(iter_message_texts(messages, system, tools)))
            measurement_out = kwargs.get('measurement_out')
            selected_text_token_counts = None
            if isinstance(measurement_out, dict):
                selected_text_token_counts = {}
                for message in messages or ():
                    if (not isinstance(message, dict)
                            or message.get('role') != 'tool'):
                        continue
                    content = message.get('content')
                    if not isinstance(content, str):
                        continue
                    if len(selected_text_token_counts) >= (
                            REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_MAX):
                        break
                    selected_text_token_counts[id(content)] = None
            if view is not None:
                total += _count_request_with_view(
                    messages, system, tools,
                    encoding=encoding,
                    encoder=enc,
                    selected_text_token_counts=selected_text_token_counts,
                    view=view,
                )
            elif texts:
                total += _count_request_texts_with_cache(
                    texts,
                    model=model,
                    encoder=enc,
                    selected_text_token_counts=selected_text_token_counts,
                )
            total += per_msg_overhead * (len(messages) if messages else 0)
            total += count_images(messages) * _IMAGE_TOKENS_DEFAULT
            total += _STRUCTURAL_OVERHEAD_TOKENS
            if (selected_text_token_counts is not None
                    and all(count is not None
                            for count in selected_text_token_counts.values())):
                measurement_out[
                    REUSABLE_TEXT_TOKEN_COUNTS_BY_IDENTITY_KEY
                ] = {
                    identity: int(count)
                    for identity, count in selected_text_token_counts.items()
                    if count is not None
                }
            return total
        except Exception as e:
            logger.warning('[TokenCounter] tiktoken count failed: %s', e)
            return None


__all__ = [
    'REQUEST_TEXT_VIEW', 'RequestTextCountView', 'RequestTextCounts',
    'TiktokenCounter', 'count_text',
    'cached_cheap_estimate_text',
    'encoding_for_model',
    'request_text_count_view',
    'text_count_cache_snapshot',
]
