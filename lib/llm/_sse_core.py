# HOT_PATH
"""Transport-agnostic core for streaming chat completions.

Both ``lib/llm/stream.py`` (sync, ``requests``) and ``lib/llm/astream.py``
(async, ``httpx``) used to carry a ~480-line copy of the *identical* SSE
chunk-parsing loop: error classification, MiniMax ``<think>`` demux,
tool-call accumulation, premature-close / empty-stop anomaly diagnostics,
and ``usage`` metadata injection. Every fix had to land twice and the two
copies drifted.

This module holds that logic exactly once. The two transport shells keep
only what genuinely differs:

  - opening the stream + iterating lines (``requests`` vs ``httpx``);
  - the retry/backoff loop's sleep call (blocking vs ``await``);
  - mapping ``httpx`` transport exceptions to ``RetryableAPIError``.

Public surface
--------------
  - ``prepare_request(body, *, attempt, log_prefix, api_key, base_url,
    extra_headers) -> RequestPlan`` — the identical pre-flight (cache
    breakpoints, extended-TTL header, Codex translation, header build,
    URL resolution, RawSSEDumper start).
  - ``classify_status_error(status_code, err_text, *, body, log_prefix,
    raw_dumper)`` — shared non-200 handling (delegates to
    ``_classify_http_error``); the caller reads the error body in its own
    transport-native way and passes the text in.
  - ``SSEAccumulator`` — feed it one raw SSE line at a time via
    ``feed_line(line)`` (returns ``True`` when the stream should stop),
    then call ``finalize(...)`` for the ``(msg, finish_reason, usage)``
    tuple. ``feed_line`` raises the same exceptions the inline loop did
    (``ModelLimitError`` / ``RateLimitError`` / ``PromptTooLongError`` /
    ``RetryableAPIError`` / ``Exception('SSE error: …')``), so the
    transport shell's retry wrapper handles them unchanged.

This is a pure code-motion refactor: no retry cap, timeout, backoff, or
cache constant is changed here. The ``usage`` anomaly fields
(``_chunks_received``, ``_stream_anomaly``, ``_missing_done``,
``_missing_finish_reason``, ``_empty_stop``, ``stream_elapsed_ms``,
``trace_id``, ``resp_trace_id``) are emitted byte-for-byte as before
because ``lib/tasks_pkg/stream_handler.py::analyse_stream_result`` keys
its retry buckets off them.
"""

import json
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

import lib as _lib
from lib.llm._transport import chat_url, headers
from lib.llm.cache import add_cache_breakpoints
from lib.llm.diagnostics import RawSSEDumper
from lib.llm_errors import (
    ModelLimitError,
    PromptTooLongError,
    RateLimitError,
    RetryableAPIError,
    _GATEWAY_THROTTLE_STATUS,
    _classify_http_error,
    _is_prompt_too_long,
)
from lib.log import get_logger
from lib.model_info import (
    _learn_model_limit,
    _parse_token_limit_from_error,
    is_claude,
    is_minimax,
)

logger = get_logger(__name__)

_INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')
_MAX_CONSECUTIVE_PARSE_ERRORS = 10

# ── Cache byte-probe (diagnostic, default OFF, zero production impact) ──
# When TOFU_CACHE_BYTE_PROBE is set to a conv-id prefix, prepare_request dumps
# the FINAL post-translation body (the exact messages+system+tools bytes handed
# to the transport, AFTER add_cache_breakpoints AND openai_body_to_anthropic)
# for the matching conversation, on each round, to
# ``.tofu_cache_probe/<conv>/round_NNNN_<trace>.json``. A standalone analyzer
# (debug/cache_byte_probe_diff.py) then diffs two consecutive rounds at the RAW
# byte level — deliberately NOT through canonical_messages — to settle whether
# a "PROVEN server-side" cache miss is actually a client-caused prefix mutation
# the canonical fingerprint erased. Unset ⇒ the whole block is skipped.
_CACHE_PROBE_ROUND: dict = {}


def _cache_probe_stable_ttls(body):
    """Collect every ``cache_control`` marker's ttl + a coarse location, so the
    analyzer can tell a stable-block ttl flip (1h↔absent) from a body change.

    Returns a list of ``{loc, ttl}`` in wire order. ``ttl`` is ``''`` for a
    bare ``{'type':'ephemeral'}`` marker (5-minute default). Best-effort.
    """
    out = []

    def _scan(container, loc):
        if isinstance(container, dict):
            cc = container.get('cache_control')
            if isinstance(cc, dict):
                out.append({'loc': loc, 'ttl': cc.get('ttl', '')})
            content = container.get('content')
            if isinstance(content, list):
                for j, blk in enumerate(content):
                    _scan(blk, f'{loc}.content[{j}]')
        # else: str content carries no marker

    # Anthropic path: system + tools live at the top level; messages below.
    sysblk = body.get('system')
    if isinstance(sysblk, list):
        for i, blk in enumerate(sysblk):
            _scan(blk, f'system[{i}]')
    tools = body.get('tools')
    if isinstance(tools, list):
        for i, t in enumerate(tools):
            _scan(t, f'tools[{i}]')
    for i, m in enumerate(body.get('messages') or []):
        _scan(m, f'messages[{i}]')
    return out


def _maybe_dump_cache_probe(body, task_id, log_prefix='', routing=None):
    """Dump the final post-translation body for a targeted conv (diagnostic).

    Gated on ``TOFU_CACHE_BYTE_PROBE`` (a conv-id prefix). Resolves the conv id
    from ``task_id`` via the chat runtime, and only dumps when it matches the
    target. Best-effort: any failure is logged at debug and never blocks a
    request. This does NOT canonicalize — it writes the literal body dict so
    the analyzer sees the exact wire bytes.

    ``routing`` (optional) carries the per-request routing fingerprint — key
    discriminator, endpoint, final ``anthropic-beta`` header — so a raw-byte
    round-over-round diff can distinguish a BODY-byte flip from a cache-NAMESPACE
    change (same bytes routed to a different key/endpoint → different gateway
    cache pool → floor miss on an otherwise byte-identical prefix). This is the
    dimension the mrne3bqe R4 clean-round miss (byte-identical, no retry) needs.
    """
    import os
    target = os.environ.get('TOFU_CACHE_BYTE_PROBE', '').strip()
    if not target:
        return
    try:
        conv_id = ''
        if task_id:
            try:
                from lib.tasks_pkg.manager._state import _chat_runtime
                _t = _chat_runtime.get(task_id)
                if _t:
                    conv_id = _t.get('convId') or ''
            except Exception as _re:
                logger.debug('%s cache-probe conv resolve failed: %s', log_prefix, _re)
        # Match on conv-id prefix; if the conv is unknown, fall back to task id
        # so a probe can still target a task that isn't in the conv index.
        key = conv_id or task_id
        if not key or not key.startswith(target):
            return

        import json as _json
        import time as _time
        from lib.agent_artifacts import ARTIFACT_PREFIX
        base = os.path.join(os.getcwd(), f'{ARTIFACT_PREFIX}_cache_probe', key)
        os.makedirs(base, exist_ok=True)
        rnd = _CACHE_PROBE_ROUND.get(key, 0)
        _CACHE_PROBE_ROUND[key] = rnd + 1
        # Dump the exact system/messages/tools that go on the wire. Use the
        # SAME serialization the transport uses (ensure_ascii=False) so byte
        # lengths match what is actually sent.
        snapshot = {
            'round': rnd,
            'ts': _time.time(),
            'conv_id': conv_id,
            'task_id': task_id,
            'model': body.get('model', ''),
            # ── Routing fingerprint (cache-NAMESPACE dimension) ──
            # Same body bytes routed to a different key/endpoint land in a
            # different gateway cache pool → floor miss on a byte-identical
            # prefix (the mrne3bqe R4 clean-round hypothesis). The API key is
            # NEVER dumped raw — only a short salted hash as a stable "which
            # key" discriminator (CLAUDE.md §2.6: never log secrets).
            'routing': routing or {},
            # Stable-block cache_control ttl values, in wire order. A 1h↔absent
            # flip here shifts the Anthropic cache key even when body bytes and
            # marker COUNT are unchanged (the detector's historical blind spot).
            'stable_ttls': _cache_probe_stable_ttls(body),
            'system': body.get('system'),
            'tools': body.get('tools'),
            'messages': body.get('messages') or [],
        }
        path = os.path.join(base, f'round_{rnd:04d}.json')
        with open(path, 'w', encoding='utf-8') as fh:
            _json.dump(snapshot, fh, ensure_ascii=False)
        logger.warning('%s [CacheProbe] dumped round=%d conv=%s → %s',
                       log_prefix, rnd, key[:12], path)
    except Exception as e:
        logger.debug('%s cache byte-probe dump failed: %s', log_prefix, e)


@dataclass
class RequestPlan:
    """Everything a transport shell needs to open the stream."""
    url: str
    hdrs: dict
    body: dict
    trace_id: str
    raw_dumper: RawSSEDumper
    codex_translator: Any
    t0: float
    anthropic_translator: Any = None


def prepare_request(body, *, attempt=0, log_prefix='', api_key=None,
                    base_url=None, extra_headers=None,
                    api_protocol='openai', oauth='') -> RequestPlan:
    """Identical pre-flight for both transports.

    Mutates ``body`` in place (cache breakpoints, ``_task_id`` pop, Codex
    translation) exactly as the inline code did, then returns the plan.
    """
    # Read the latch key NON-destructively and keep it on the body for the
    # WHOLE task life. The streaming retry loop re-feeds the SAME body dict to
    # this function on every 429/503 attempt (see lib/llm/stream.py:62); popping
    # _task_id on attempt 1 made attempt 2+ fall back to the live global
    # CACHE_EXTENDED_TTL, flipping the cache_control ttl AND the beta header
    # below → a different Anthropic cache key → full prefix miss. _task_id must
    # NOT reach the gateway on the OpenAI path (raw body is serialized), so it
    # is stripped at that serialization boundary instead (see below). The
    # Anthropic path rebuilds the body from an allowlist, so it never leaks.
    _task_id_for_latch = body.get('_task_id', '')
    add_cache_breakpoints(body, log_prefix)

    # Auto-inject extended cache TTL beta header for Claude
    if is_claude(body.get('model', '')):
        if _task_id_for_latch:
            from lib.tasks_pkg.cache_tracking import latch_extended_ttl
            _use_ext_ttl = latch_extended_ttl(_task_id_for_latch)
        else:
            _use_ext_ttl = getattr(_lib, 'CACHE_EXTENDED_TTL', False)
        if _use_ext_ttl:
            if extra_headers is None:
                extra_headers = {}
            _existing_beta = extra_headers.get('anthropic-beta', '')
            _ttl_beta = 'extended-cache-ttl-2025-04-11'
            if _ttl_beta not in _existing_beta:
                if _existing_beta:
                    extra_headers['anthropic-beta'] = f'{_existing_beta},{_ttl_beta}'
                else:
                    extra_headers['anthropic-beta'] = _ttl_beta

    # Subscription-OAuth slot: swap in a live token + client-identity headers,
    # and (for Claude) prepend the mandatory identity system block — all BEFORE
    # the body translation below reads messages / builds headers.
    if oauth:
        from lib.oauth.outbound import resolve_oauth_request
        api_key, extra_headers, body = resolve_oauth_request(oauth, body, extra_headers)

    # Codex OAuth translation
    codex_translator = None
    anthropic_translator = None
    if base_url and 'codex' in base_url and 'chatgpt.com' in base_url:
        from lib.oauth.codex import codex_translate_request, CodexSSETranslator
        body = codex_translate_request(body)
        codex_translator = CodexSSETranslator(model=body.get('model', ''))
        url = f'{base_url.rstrip("/")}/responses'
        logger.debug('%s [Codex] Translated request for Responses API', log_prefix)
    elif api_protocol == 'anthropic':
        from lib.llm.anthropic_outbound import (
            AnthropicSSETranslator, anthropic_messages_url,
            openai_body_to_anthropic,
        )
        _model_name = body.get('model', '')
        body = openai_body_to_anthropic(body)
        anthropic_translator = AnthropicSSETranslator(model=_model_name)
        url = anthropic_messages_url(base_url)
        if oauth == 'claude':
            from lib.oauth.outbound import claude_oauth_url
            url = claude_oauth_url(url)
        logger.debug('%s [Anthropic] Translated request for Messages API', log_prefix)
    else:
        # OpenAI path serialises `body` verbatim (session.post(json=body)), so
        # the internal latch key must be removed HERE — the single serialization
        # boundary — rather than popped early (which broke the retry-stable
        # latch, see above). The Anthropic/Codex branches rebuilt `body` from an
        # allowlist that never included _task_id, so this only matters here.
        body.pop('_task_id', None)
        url = f'{base_url.rstrip("/")}/chat/completions' if base_url else chat_url()

    attempt_tag = f' (attempt {attempt+1})' if attempt > 0 else ''
    if log_prefix:
        logger.debug('%s%s POST %s msgs=%d tools=%s', log_prefix, attempt_tag, url,
                     len(body.get('messages', [])), 'yes' if body.get('tools') else 'no')

    trace_id = uuid.uuid4().hex
    if anthropic_translator is not None:
        from lib.llm.anthropic_outbound import anthropic_headers
        hdrs = anthropic_headers(api_key, extra_headers)
        if oauth == 'claude':
            # Subscription tokens are rejected on Authorization: Bearer
            # (401 since 2026); the token must ride x-api-key only.
            hdrs.pop('Authorization', None)
    else:
        hdrs = headers()
        if api_key:
            hdrs['Authorization'] = f'Bearer {api_key}'
        if extra_headers:
            hdrs.update(extra_headers)
    hdrs['M-TraceId'] = trace_id

    if log_prefix:
        logger.debug('%s M-TraceId=%s', log_prefix, trace_id)

    t0 = time.time()
    raw_dumper = RawSSEDumper(body.get('model', ''), trace_id, body)
    raw_dumper.start()

    # ── Wire fingerprint (cache-miss traceability) ──
    # This is the ONLY point that sees the FINAL, post-translation messages
    # exactly as they go on the wire (after add_cache_breakpoints AND, on the
    # anthropic path, openai_body_to_anthropic). Canonicalise them into an
    # envelope-agnostic fingerprint and stash it on the RawSSEDumper (which
    # travels into SSEAccumulator → finalize, where it is relayed into `usage`
    # like trace_id). Stashing it on the dumper — NOT on `body` — keeps the
    # ephemeral fingerprint OFF the wire (body is what requests/httpx serialise).
    # detect_cache_break then PROVES a server-side miss (bytes identical) vs.
    # names a client-caused culprit. Best-effort: never block a request.
    raw_dumper.wire_fp = None
    raw_dumper.wire_static = ''
    raw_dumper.wire_system = None
    raw_dumper.wire_markers = None
    raw_dumper.wire_bytes = None
    raw_dumper.wire_field_bytes = None
    raw_dumper.wire_region = None
    try:
        from lib.tasks_pkg.wire_fingerprint import (
            canonical_messages, marker_signature, static_prefix_hash,
            system_fingerprint, wire_byte_field_prefix, wire_byte_prefix,
            wire_byte_region,
        )
        raw_dumper.wire_fp = canonical_messages(body.get('messages') or [])
        raw_dumper.wire_static = static_prefix_hash(body.get('messages') or [])
        # TRUE-byte prefix: hash the ACTUAL serialized bytes per message (only
        # cache_control stripped). canonical_messages is lossy (drops
        # reasoning_details, collapses str↔block, canonicalises arg order), so
        # "canonical identical" does NOT prove "wire bytes identical". This lets
        # detect_cache_break REFUSE a false "byte-identical eviction" claim when
        # the real bytes diverged (reasoning_details rebuild / same-role merge /
        # protocol switch) — see wire_byte_prefix's docstring.
        raw_dumper.wire_bytes = wire_byte_prefix(body.get('messages') or [])
        # FIELD-GRANULAR true bytes: names the EXACT top-level field that
        # flipped on a canonical-invisible <bytes> divergence (reasoning_details
        # rebuild / tool_calls arg re-serialization / content / field-order),
        # so detect_cache_break can log the proven field instead of only the
        # message. See wire_byte_field_prefix.
        raw_dumper.wire_field_bytes = wire_byte_field_prefix(
            body.get('messages') or [])
        # TRUE-byte hash of the HOISTED system + tools region. system_fingerprint
        # is ITSELF lossy (runs _text_of + sort_keys on params), so a system
        # BLOCK REORDER / wrapping flip / per-turn re-serialization — the
        # highest-probability suspect on the Anthropic path, where charter /
        # board / peer-status / relevant_memories are injected fresh each turn —
        # is invisible to it. This hashes the real serialized bytes so that
        # divergence can't be laundered into "eviction". See wire_byte_region.
        raw_dumper.wire_region = wire_byte_region(
            body.get('system'), body.get('tools'))
        # Capture WHERE the cache_control breakpoints sit — canonical_messages
        # deliberately strips them, so a miss caused purely by a breakpoint
        # being LOST in translation (byte-identical content) would otherwise be
        # mislabeled "server-side PROVEN". detect_cache_break folds this in.
        raw_dumper.wire_markers = marker_signature(body)
        # Also fingerprint the HOISTED system block + tools. On the Anthropic
        # path these live OUTSIDE body['messages'] (openai_body_to_anthropic
        # lifts system to the top-level field), so canonical_messages is blind
        # to them — a per-turn system change (digest/charter/board) was
        # laundered into a false "server-side PROVEN" verdict. This closes that
        # blind spot.
        raw_dumper.wire_system = system_fingerprint(
            body.get('system'), body.get('tools'))
    except Exception as _wfe:
        logger.debug('%s wire fingerprint capture failed: %s', log_prefix, _wfe)

    # Diagnostic byte-probe (default OFF): dump the exact post-translation body
    # for a targeted conv so a raw-byte round-over-round diff can settle whether
    # a "server-side PROVEN" miss is actually a client-caused prefix mutation.
    # Also capture the ROUTING fingerprint — key discriminator / endpoint /
    # final anthropic-beta — so the diff can tell a body-byte flip from a
    # cache-namespace (key/endpoint) change on a byte-identical prefix.
    _routing = None
    try:
        import hashlib as _hashlib
        _key_hash = ''
        if api_key:
            _key_hash = _hashlib.sha256(
                ('tofu-cache-probe:' + str(api_key)).encode('utf-8')
            ).hexdigest()[:12]
        _sticky = {}
        try:
            from lib.llm_dispatch.conv_affinity import get_pick_decision
            _sticky = get_pick_decision() or {}
        except Exception as _se:
            logger.debug('%s cache-probe sticky capture failed: %s', log_prefix, _se)
        _routing = {
            'url': url,
            'base_url': base_url or '',
            'key_hash': _key_hash,           # salted, truncated — NOT the secret
            'anthropic_beta': (hdrs.get('anthropic-beta', '')
                               if isinstance(hdrs, dict) else ''),
            'trace_id': trace_id,
            'attempt': attempt,
            'api_protocol': api_protocol,
            # Sticky-routing decision (WHY the key is what it is): distinguishes
            # a soft-fallback-under-cooldown flip (affinity_fell_back=True) from
            # affinity never engaging. {preferred_key_hash, chosen_key_hash,
            # affinity_fell_back, cooldown_remaining_s}.
            'sticky': _sticky,
        }
    except Exception as _rfe:
        logger.debug('%s cache-probe routing capture failed: %s', log_prefix, _rfe)

    # ── ALWAYS-ON cache-namespace routing fingerprint (relayed into usage) ──
    # The byte-probe above is default-OFF, so historically the routing captured
    # here reached NOTHING at runtime and the cache-miss verdict was blind to a
    # key/beta/endpoint flip — mislabeling a client cache-namespace switch as a
    # server-side miss. Distil _routing down to the three attributes that
    # DETERMINE the gateway cache namespace and stash them on the dumper so
    # SSEAccumulator.finalize relays them into `usage['_wire_routing']` UNCONDI-
    # TIONALLY (like _wire_fp). detect_cache_break diffs it round-over-round and
    # NAMES a namespace switch instead of blaming the gateway. Best-effort: a
    # capture failure leaves wire_routing=None → the detector's diff is inert.
    raw_dumper.wire_routing = None
    if _routing is not None:
        try:
            from lib.tasks_pkg.wire_fingerprint import routing_fingerprint
            raw_dumper.wire_routing = routing_fingerprint(
                key_hash=_routing.get('key_hash', ''),
                anthropic_beta=_routing.get('anthropic_beta', ''),
                endpoint=_routing.get('url', ''))
        except Exception as _rfpe:
            logger.debug('%s routing fingerprint build failed: %s',
                         log_prefix, _rfpe)

    _maybe_dump_cache_probe(body, _task_id_for_latch, log_prefix, routing=_routing)

    return RequestPlan(url=url, hdrs=hdrs, body=body, trace_id=trace_id,
                       raw_dumper=raw_dumper, codex_translator=codex_translator,
                       t0=t0, anthropic_translator=anthropic_translator)


def classify_status_error(status_code, err_text, *, body, log_prefix, raw_dumper):
    """Shared non-200 handling. Caller reads the body text per-transport.

    Always raises (via ``_classify_http_error``) — never returns normally
    when ``status_code != 200``.
    """
    err_msg = f'API HTTP {status_code}: {err_text[:800]}'
    if raw_dumper.enabled:
        raw_dumper.line(f'[HTTP-{status_code}] {err_text[:2000]}')
    _classify_http_error(status_code, err_msg, body.get('model', ''),
                         log_prefix, max_tokens=body.get('max_tokens', 0))


class SSEAccumulator:
    """Transport-agnostic SSE chunk parser + assistant-message builder.

    Usage::

        acc = SSEAccumulator(body, trace_id, raw_dumper, codex_translator,
                             t0, log_prefix=..., on_thinking=..., ...)
        for raw_line in transport_lines():
            if abort_check and abort_check():
                acc.mark_aborted(); break
            if acc.feed_line(raw_line):   # True → saw [DONE]
                break
        acc.fire_final_tool_callback()
        msg, finish_reason, usage = acc.finalize(resp_trace=...)
    """

    def __init__(self, body, trace_id, raw_dumper, codex_translator, t0, *,
                 url='', log_prefix='', on_thinking=None, on_content=None,
                 on_tool_call_ready=None, anthropic_translator=None):
        self.body = body
        self.trace_id = trace_id
        self.raw_dumper = raw_dumper
        self.codex_translator = codex_translator
        self.anthropic_translator = anthropic_translator
        self.t0 = t0
        self.url = url
        self.log_prefix = log_prefix
        self.on_thinking = on_thinking
        self.on_content = on_content
        self.on_tool_call_ready = on_tool_call_ready

        self.content = ''
        self.thinking_text = ''
        self.thinking_signature = ''
        self.tool_calls_acc: dict = {}
        self.finish_reason = 'stop'
        self.usage: Optional[dict] = None
        self.saw_done = False
        self.saw_finish_reason = False
        self.chunk_count = 0
        self.aborted_by_client = False

        self._mm_mode = is_minimax(body.get('model', ''))
        self._mm_in_think = False
        self._mm_buf = ''
        self._consecutive_parse_errors = 0

    def mark_aborted(self):
        self.aborted_by_client = True
        logger.debug('%s Stream aborted by client after %d chunks',
                     self.log_prefix, self.chunk_count)

    def feed_line(self, line) -> bool:
        """Process one raw SSE line. Returns True when the stream should stop.

        Mirrors the inline per-line handling exactly: dumps the raw line,
        skips non-``data:`` lines, detects ``[DONE]``, and dispatches the
        JSON chunk. Raises the same typed exceptions on SSE error objects.
        """
        self.raw_dumper.line(line if line is not None else '')
        if not line or not line.startswith('data:'):
            return False
        data_str = line[5:].strip()
        if data_str == '[DONE]':
            self.saw_done = True
            return True
        if not data_str:
            return False
        self.chunk_count += 1

        # Codex SSE translation
        if self.codex_translator is not None:
            return self._feed_codex(data_str)

        # Anthropic SSE translation → OpenAI chunks
        if self.anthropic_translator is not None:
            return self._feed_anthropic(data_str)

        try:
            chunk = json.loads(data_str)
        except Exception as e:
            self._consecutive_parse_errors += 1
            logger.warning('%s ⚠ SSE chunk JSON parse error (chunk #%d, consecutive=%d) '
                           'model=%s trace=%s: %s — %s',
                           self.log_prefix, self.chunk_count, self._consecutive_parse_errors,
                           self.body.get('model', '?'), self.trace_id, data_str[:200], e,
                           exc_info=True)
            if self._consecutive_parse_errors >= _MAX_CONSECUTIVE_PARSE_ERRORS:
                self.raw_dumper.dump_anomaly(
                    'parse_error',
                    consecutive_errors=self._consecutive_parse_errors,
                    chunk_count=self.chunk_count,
                    last_data_preview=data_str[:200],
                    model=self.body.get('model', '?'),
                )
                raise RetryableAPIError(
                    f'{self._consecutive_parse_errors} consecutive SSE parse errors '
                    f'— stream appears corrupt') from e
            return False

        self._consecutive_parse_errors = 0
        self._process_openai_chunk(chunk)
        return False

    def _process_openai_chunk(self, chunk):
        """Accumulate one OpenAI-shaped chat.completion chunk dict."""
        if 'error' in chunk:
            self._handle_sse_error(chunk['error'])

        if chunk.get('usage'):
            self.usage = chunk['usage']

        choices = chunk.get('choices', [])
        if not choices:
            return

        delta = choices[0].get('delta', {})
        fr = choices[0].get('finish_reason')
        if fr:
            self.finish_reason = fr
            self.saw_finish_reason = True
        if choices[0].get('usage'):
            self.usage = choices[0]['usage']

        self._handle_delta(delta)

    def _feed_anthropic(self, data_str) -> bool:
        """Translate one Anthropic SSE payload into OpenAI chunks + accumulate.

        Returns True when the translator emits the ``[DONE]`` sentinel.
        """
        for chunk in self.anthropic_translator.translate(data_str):
            if chunk == '[DONE]':
                self.saw_done = True
                return True
            self._process_openai_chunk(chunk)
        return False

    def _feed_codex(self, data_str) -> bool:
        """Translate a Codex Responses-API SSE payload and accumulate.

        The Codex translator emits OpenAI-shaped ``chat.completion.chunk``
        JSON strings, so route them through the SAME ``_process_openai_chunk``
        path the main OpenAI and Anthropic (``_feed_anthropic``) paths use.
        Sharing that one accumulator keeps content / thinking / tool-call-delta
        accumulation, ``on_tool_call_ready`` firing, and ``usage`` handling
        byte-identical across every provider — the Codex path previously
        re-implemented the accumulation and, in doing so, (1) never fired
        ``on_tool_call_ready`` (no incremental multi-tool prefetch) and
        (2) gated content/thinking *accumulation* on the callback being present
        (``if _c and self.on_content``), silently dropping the whole response
        for a caller with no streaming callback.

        Returns True when ``[DONE]`` was seen inside the translation.
        """
        for t_str in self.codex_translator.translate(data_str):
            if t_str == '[DONE]':
                self.saw_done = True
                return True
            try:
                t_chunk = json.loads(t_str)
            except Exception as e:
                logger.debug('[LLM] Codex SSE chunk parse failed: %s', e)
                continue
            self._process_openai_chunk(t_chunk)
        return False

    def _handle_sse_error(self, eo):
        """Classify an SSE-embedded error object; always raises."""
        err_text = eo.get('message', '') if isinstance(eo, dict) else str(eo)
        _err_lower = err_text.lower()
        _model_id = self.body.get('model', '')
        _detected_limit = _parse_token_limit_from_error(err_text, _model_id)
        if _detected_limit:
            _learn_model_limit(_model_id, _detected_limit)
            raise ModelLimitError(
                f'SSE error (token limit): {err_text}',
                _model_id, _detected_limit,
                self.body.get('max_tokens', 0))
        if _is_prompt_too_long(err_text):
            logger.warning('%s Prompt too long detected in SSE error: %s',
                           self.log_prefix, err_text[:300])
            raise PromptTooLongError(f'SSE error: {err_text}')
        _sse_err_type = eo.get('type', '') if isinstance(eo, dict) else ''
        _sse_http_code = str(eo.get('http_code', '')) if isinstance(eo, dict) else ''
        # ★ Some upstream gateways (AWS Bedrock, GCP Vertex) embed the HTTP
        #   status inside the message text instead of a structured field,
        #   e.g. "(Service: BedrockRuntime, Status Code: 429, …)".
        if not _sse_http_code:
            _m = re.search(r'status code[:\s]+(\d{3})', _err_lower)
            if _m:
                _sse_http_code = _m.group(1)
        _sse_quota_patterns = [
            'too many tokens', 'too many requests',
            'quota exceeded', 'rate exceeded',
            'tokens per day', 'tokens per minute',
            'requests per day', 'requests per minute',
            'throttling', 'throttled',
        ]
        _sse_retryable_patterns = [
            '负载较高', 'server overload', 'service overload',
            'capacity', 'try again later', '稍后重试',
            'temporarily unavailable',
        ]
        _sse_non_retryable_patterns = [
            'not support model', 'invalid api key',
            'unauthorized', 'forbidden', 'not found',
            'plan not support', 'permission denied',
        ]
        _is_sse_non_retryable = any(p in _err_lower for p in _sse_non_retryable_patterns)
        _is_sse_quota = (
            not _is_sse_non_retryable
            and (
                _sse_http_code == '429'
                or any(p in _err_lower for p in _sse_quota_patterns)
            )
        )
        _is_sse_retryable = (
            not _is_sse_non_retryable
            and not _is_sse_quota
            and (
                _sse_err_type == 'server_error'
                or _sse_http_code.startswith('5')
                or any(p in _err_lower for p in _sse_retryable_patterns)
            )
        )
        if _is_sse_quota:
            logger.warning('%s SSE rate-limit/quota detected — escalating to '
                           'dispatch layer: %s', self.log_prefix, err_text[:300])
            raise RateLimitError(
                f'SSE error: {err_text}',
                reason=f'HTTP 429: {err_text[:180]}')
        if _is_sse_retryable:
            _sse_status = int(_sse_http_code) if _sse_http_code.isdigit() else 500
            if _sse_status in _GATEWAY_THROTTLE_STATUS:
                logger.warning('%s SSE gateway throttle (HTTP %d) — escalating to '
                               'dispatch layer: %s', self.log_prefix, _sse_status,
                               err_text[:300])
                raise RateLimitError(
                    f'SSE error: {err_text}', is_gateway=True,
                    reason=f'HTTP {_sse_status}: {err_text[:180]}')
            logger.warning('%s SSE server error (retryable): %s',
                           self.log_prefix, err_text[:300])
            raise RetryableAPIError(
                f'SSE error: {err_text}',
                status_code=_sse_status)
        if not err_text:
            err_text = (f'<empty error body> sse_type={_sse_err_type or "?"} '
                        f'http_code={_sse_http_code or "?"} '
                        f'model={self.body.get("model", "?")} '
                        f'trace={self.trace_id}')
        raise Exception(f'SSE error: {err_text}')

    def _handle_delta(self, delta):
        """Accumulate thinking / content / tool-call deltas from one chunk."""
        # Thinking / reasoning delta
        td = (delta.get('thinking')
              or delta.get('reasoning_content')
              or (delta.get('content', '')
                  if delta.get('role') == 'thinking' else ''))
        # OpenRouter-style ``reasoning_details`` carry both the thinking text
        # and the opaque Claude signature, in separate chunks:
        #   [{"type":"thinking","thinking":"…"}]    ← text delta
        #   [{"type":"thinking","signature":"…"}]   ← signature (once per turn)
        # The Meituan/sankuai OpenAI-compat gateway uses exactly this shape
        # for Claude models, so harvest both keys here.
        rd_parts = delta.get('reasoning_details')
        if isinstance(rd_parts, list):
            if not td:
                td = ''.join(
                    (d.get('thinking') or d.get('text') or '')
                    for d in rd_parts if isinstance(d, dict))
            for d in rd_parts:
                if isinstance(d, dict) and d.get('signature'):
                    self.thinking_signature += d['signature']
        if td:
            self.thinking_text += td
            if self.on_thinking:
                self.on_thinking(td)

        # Opaque thinking-block signature (Anthropic Messages API path).
        # Surfaced by the AnthropicSSETranslator as a synthetic delta field;
        # needed to replay the thinking block on a later tool-use turn.
        _tsig = delta.get('thinking_signature')
        if _tsig:
            self.thinking_signature += _tsig

        # Content delta
        if 'content' in delta and delta.get('role') != 'thinking':
            cd = delta['content'] or ''
            if cd:
                if self._mm_mode:
                    self._feed_minimax(cd)
                else:
                    self.content += cd
                    if self.on_content:
                        self.on_content(cd)

        # Tool call deltas
        _tc_list = delta.get('tool_calls') or []
        if _tc_list:
            for tc in _tc_list:
                idx = tc.get('index', 0)
                if idx not in self.tool_calls_acc:
                    if self.on_tool_call_ready and idx > 0 and (idx - 1) in self.tool_calls_acc:
                        _prev = self.tool_calls_acc[idx - 1]
                        try:
                            self.on_tool_call_ready(_prev)
                        except Exception as _tcr_err:
                            logger.debug('%s on_tool_call_ready callback error: %s',
                                         self.log_prefix, _tcr_err)
                    self.tool_calls_acc[idx] = {
                        'id': '', 'type': 'function',
                        'function': {'name': '', 'arguments': ''},
                    }
                if tc.get('id'):
                    self.tool_calls_acc[idx]['id'] = tc['id']
                if tc.get('extra_content'):
                    self.tool_calls_acc[idx]['extra_content'] = tc['extra_content']
                fn = tc.get('function', {})
                if fn.get('name'):
                    self.tool_calls_acc[idx]['function']['name'] += fn['name']
                if fn.get('arguments') is not None:
                    self.tool_calls_acc[idx]['function']['arguments'] += fn.get('arguments', '')

    def _feed_minimax(self, cd):
        """MiniMax inline ``<think>…</think>`` demux into thinking vs content."""
        self._mm_buf += cd
        while self._mm_buf:
            if self._mm_in_think:
                end_idx = self._mm_buf.find('</think>')
                if end_idx == -1:
                    self.thinking_text += self._mm_buf
                    if self.on_thinking:
                        self.on_thinking(self._mm_buf)
                    self._mm_buf = ''
                else:
                    think_part = self._mm_buf[:end_idx]
                    if think_part:
                        self.thinking_text += think_part
                        if self.on_thinking:
                            self.on_thinking(think_part)
                    self._mm_buf = self._mm_buf[end_idx + len('</think>'):]
                    self._mm_in_think = False
            else:
                start_idx = self._mm_buf.find('<think>')
                if start_idx == -1:
                    if len(self._mm_buf) > 7 and '<' in self._mm_buf[-7:]:
                        safe = self._mm_buf[:self._mm_buf.rfind('<', max(0, len(self._mm_buf) - 7))]
                        if safe:
                            self.content += safe
                            if self.on_content:
                                self.on_content(safe)
                        self._mm_buf = self._mm_buf[len(safe):]
                    else:
                        self.content += self._mm_buf
                        if self.on_content:
                            self.on_content(self._mm_buf)
                        self._mm_buf = ''
                else:
                    before = self._mm_buf[:start_idx]
                    if before:
                        self.content += before
                        if self.on_content:
                            self.on_content(before)
                    self._mm_buf = self._mm_buf[start_idx + len('<think>'):]
                    self._mm_in_think = True

    def fire_final_tool_callback(self):
        """Fire on_tool_call_ready for the LAST accumulated tool call."""
        if self.on_tool_call_ready and self.tool_calls_acc:
            _last_idx = max(self.tool_calls_acc.keys())
            _last_tc = self.tool_calls_acc[_last_idx]
            if _last_tc['function']['name']:
                try:
                    self.on_tool_call_ready(_last_tc)
                except Exception as _tcr_err:
                    logger.debug('%s on_tool_call_ready callback error (final): %s',
                                 self.log_prefix, _tcr_err)

    def finalize(self, *, resp_trace=''):
        """Flush buffers, build the assistant msg, emit diagnostics + usage.

        Returns ``(msg, finish_reason, usage)`` — identical shape and
        anomaly fields to the former inline implementation.
        """
        # Flush MiniMax buffer
        if self._mm_mode and self._mm_buf:
            if self._mm_in_think:
                self.thinking_text += self._mm_buf
                if self.on_thinking:
                    self.on_thinking(self._mm_buf)
            else:
                self.content += self._mm_buf
                if self.on_content:
                    self.on_content(self._mm_buf)
            self._mm_buf = ''

        # MiniMax: normalize reasoning_tokens into usage
        if self._mm_mode and self.usage and self.thinking_text:
            ctd = self.usage.get('completion_tokens_details', {})
            rt = ctd.get('reasoning_tokens', 0)
            if rt > 0 and 'reasoning_tokens' not in self.usage:
                self.usage['reasoning_tokens'] = rt

        # Filter out spurious tool calls
        if self.tool_calls_acc:
            _filtered = {}
            _names_with_args = {
                tc['function']['name']
                for tc in self.tool_calls_acc.values()
                if (tc['function'].get('arguments', '') or '').strip()
            }
            for idx, tc_entry in self.tool_calls_acc.items():
                fn_name = tc_entry['function']['name']
                fn_args_str = tc_entry['function'].get('arguments', '')
                if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
                    logger.debug('%s Filtering spurious internal tool call: %s',
                                 self.log_prefix, fn_name)
                    continue
                if not fn_args_str.strip() and fn_name in _names_with_args:
                    logger.warning(
                        '%s Filtering phantom tool call: %s (tc_id=%s) has '
                        'empty arguments — duplicate of another %s call with real args',
                        self.log_prefix, fn_name, tc_entry.get('id', '?')[:12], fn_name,
                    )
                    continue
                # ── Normalize empty/whitespace arguments to '{}' ──
                # A genuine no-arg tool call (or one whose args delta never
                # arrived) survives the phantom filter above with arguments=''.
                # OpenAI/Anthropic tolerate that (the executor does
                # ``json.loads(args or '{}')``), but Gemini's OpenAI-compat
                # proxy REJECTS a replayed assistant tool_call with empty
                # arguments — HTTP 400 "Expected function 'arguments' in a(n)
                # 'assistant' message to be populated." — killing the whole
                # follow-up turn. We emit '{}' (valid empty JSON object) so the
                # message replays cleanly across every provider. Equivalent to
                # the empty→'{}' coercion the DB-history replay builders already
                # apply (conv_message_builder / message_builder).
                if not fn_args_str.strip():
                    tc_entry['function']['arguments'] = '{}'
                _filtered[idx] = tc_entry
            self.tool_calls_acc = _filtered

        content = self.content
        thinking_text = self.thinking_text
        tool_calls_acc = self.tool_calls_acc
        finish_reason = self.finish_reason
        usage = self.usage

        # Build assistant message
        msg = {'role': 'assistant'}
        if thinking_text:
            msg['reasoning_content'] = thinking_text
        if self.thinking_signature:
            msg['thinking_signature'] = self.thinking_signature
        if tool_calls_acc:
            msg['tool_calls'] = [tool_calls_acc[i] for i in sorted(tool_calls_acc.keys())]
            if content:
                msg['content'] = content
        else:
            msg['content'] = content

        # Log cache info
        cache_info = ''
        if usage:
            cw = usage.get('cache_write_tokens',
                           usage.get('cache_creation_input_tokens', 0))
            cr = usage.get('cache_read_tokens',
                           usage.get('cache_read_input_tokens', 0))
            if cw or cr:
                cache_info = f' cache_w={cw} cache_r={cr}'
                if cr > 0:
                    inp = usage.get('prompt_tokens', usage.get('input_tokens', 0))
                    cache_info += f' (saved ~{round(cr / max(inp, 1) * 100)}%)'

        if self.log_prefix:
            logger.debug('%s Done: finish=%s content=%d think=%d%s', self.log_prefix,
                         finish_reason, len(content), len(thinking_text), cache_info)

        _stream_elapsed_s = time.time() - self.t0
        aborted = self.aborted_by_client
        chunk_count = self.chunk_count

        # Diagnostics: detect premature stream close
        if not aborted and not self.saw_done:
            logger.warning(
                '%s ⚠ PREMATURE STREAM CLOSE: Server never sent [DONE] marker. '
                'M-TraceId=%s resp_trace=%s elapsed=%.1fs chunks_received=%d '
                'saw_finish_reason=%s finish_reason=%s content_len=%d thinking_len=%d '
                'tool_calls=%d model=%s url=%s',
                self.log_prefix, self.trace_id, resp_trace or 'none',
                _stream_elapsed_s, chunk_count,
                self.saw_finish_reason, finish_reason,
                len(content), len(thinking_text),
                len(tool_calls_acc), self.body.get('model', '?'), self.url)
            self.raw_dumper.dump_anomaly(
                'missing_done',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                saw_finish_reason=self.saw_finish_reason,
                finish_reason=finish_reason,
                content_len=len(content),
                thinking_len=len(thinking_text),
                tool_calls=len(tool_calls_acc),
                resp_trace=resp_trace or 'none',
            )
        elif not aborted and not self.saw_finish_reason and chunk_count > 0:
            logger.warning(
                '%s ⚠ MISSING FINISH_REASON: [DONE] received but no finish_reason chunk. '
                'M-TraceId=%s elapsed=%.1fs Using default=%s chunks=%d '
                'content_len=%d model=%s',
                self.log_prefix, self.trace_id, _stream_elapsed_s,
                finish_reason, chunk_count, len(content), self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'missing_finish_reason',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                content_len=len(content),
                thinking_len=len(thinking_text),
                tool_calls=len(tool_calls_acc),
            )

        # Diagnostics: detect empty responses
        if (not aborted and finish_reason == 'stop'
                and not content and not tool_calls_acc and chunk_count > 0):
            logger.warning(
                '%s ⚠ EMPTY STOP RESPONSE: finish=stop but no content and no tool_calls. '
                'M-TraceId=%s elapsed=%.1fs chunks=%d thinking_len=%d model=%s',
                self.log_prefix, self.trace_id, _stream_elapsed_s,
                chunk_count, len(thinking_text), self.body.get('model', '?'))
            self.raw_dumper.dump_anomaly(
                'empty_stop',
                elapsed_s=round(_stream_elapsed_s, 2),
                chunks=chunk_count,
                thinking_len=len(thinking_text),
                finish_reason=finish_reason,
                resp_trace=resp_trace or 'none',
            )

        # Inject metadata into usage
        if usage is None:
            usage = {}
        usage['trace_id'] = self.trace_id
        if resp_trace and resp_trace != self.trace_id:
            usage['resp_trace_id'] = resp_trace
        usage['stream_elapsed_ms'] = round(_stream_elapsed_s * 1000)
        usage['_chunks_received'] = chunk_count

        # ── Relay the post-translation wire fingerprint into `usage` ──
        # Captured in prepare_request (the only point seeing the final wire
        # bytes) and carried on the RawSSEDumper. detect_cache_break reads these
        # to distinguish a PROVEN server-side miss (fingerprint identical to
        # last round) from a client-caused one (names the changed msg.field).
        _wfp = getattr(self.raw_dumper, 'wire_fp', None)
        if _wfp is not None:
            usage['_wire_fp'] = _wfp
            usage['_wire_static'] = getattr(self.raw_dumper, 'wire_static', '')
            _wsys = getattr(self.raw_dumper, 'wire_system', None)
            if _wsys is not None:
                usage['_wire_system'] = _wsys
            _wmk = getattr(self.raw_dumper, 'wire_markers', None)
            if _wmk is not None:
                usage['_wire_markers'] = _wmk
            _wbytes = getattr(self.raw_dumper, 'wire_bytes', None)
            if _wbytes is not None:
                usage['_wire_bytes'] = _wbytes
            _wfbytes = getattr(self.raw_dumper, 'wire_field_bytes', None)
            if _wfbytes is not None:
                usage['_wire_field_bytes'] = _wfbytes
            _wregion = getattr(self.raw_dumper, 'wire_region', None)
            if _wregion is not None:
                usage['_wire_region'] = _wregion

        # Cache-namespace routing fingerprint — relayed on its OWN guard (not
        # nested under _wfp), because routing is captured independently of the
        # body fingerprint in prepare_request, so it can be present even when
        # the message-fingerprint capture failed. detect_cache_break diffs it to
        # name a client cache-namespace switch (key/beta/endpoint flip) instead
        # of laundering a byte-identical miss into "server-side".
        _wrouting = getattr(self.raw_dumper, 'wire_routing', None)
        if _wrouting is not None:
            usage['_wire_routing'] = _wrouting

        # Stream anomaly flags
        _has_anomaly = False
        if not aborted and not self.saw_done:
            usage['_missing_done'] = True
            if not self.saw_finish_reason:
                _has_anomaly = True
        if not aborted and not self.saw_finish_reason and chunk_count > 0:
            usage['_missing_finish_reason'] = True
            _has_anomaly = True
        if (not aborted and finish_reason == 'stop'
                and not content and not tool_calls_acc and chunk_count > 0):
            usage['_empty_stop'] = True
            _has_anomaly = True
        if _has_anomaly:
            usage['_stream_anomaly'] = True

        self.raw_dumper.finish(
            finish_reason=finish_reason,
            content_len=len(content),
            thinking_len=len(thinking_text),
            tool_calls=len(tool_calls_acc),
            saw_done=self.saw_done,
            saw_finish_reason=self.saw_finish_reason,
        )

        return msg, finish_reason, usage
