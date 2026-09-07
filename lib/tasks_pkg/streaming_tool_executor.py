"""Streaming Tool Executor — start executing read-only tools while the model streams.

Inspired by Claude Code's ``StreamingToolExecutor`` (``tools/StreamingToolExecutor.ts``).
When the model emits multiple tool calls in one response, read-only tools
(``read_files``, ``grep_search``, ``find_files``, ``list_dir``, ``web_search``,
``fetch_url``) begin executing as soon as their arguments
finish streaming, rather than waiting for the complete response.

Write tools and approval-gated tools are NOT pre-executed — they are deferred
to the normal serial dispatch in ``tool_dispatch.py``.

Architecture
------------
1. The orchestrator creates a ``StreamingToolAccumulator`` before each LLM call.
2. The ``on_tool_call_ready`` callback is passed through
   ``stream_llm_response`` → ``dispatch_stream`` → ``stream_chat`` →
   ``_stream_chat_once``.
3. Each time a tool call's arguments finish during SSE streaming, the callback
   fires immediately.
4. **NEW**: The callback also immediately emits ``tool_start`` SSE events so
   the frontend can show "Searching…" / "Running…" UI without waiting for the
   entire LLM response to finish streaming.
5. If the tool is read-only and concurrency-safe, it is submitted to a thread
   pool for immediate execution — **while the model is still generating the
   next tool call**.
6. After the stream completes, the orchestrator calls ``inject_into_cache()``
   to harvest results.  Already-done results are collected immediately;
   still-running futures are **waited on** (not cancelled), since they are
   already in-progress and would be executed serially otherwise — waiting
   is strictly faster than cancelling + re-executing from scratch.
7. The results are stored in the task's ``_tool_result_cache`` dict, keyed
   exactly like ``tool_dispatch._make_cache_key``.  When
   ``execute_tool_pipeline`` runs, it finds pre-computed results in the
   dedup cache and skips re-execution.
"""

import json
import time
from collections.abc import Mapping
from concurrent.futures import Future, ThreadPoolExecutor
# See _pipeline.py: futures-TimeoutError is a distinct class on 3.10.
from concurrent.futures import TimeoutError as _FuturesTimeoutError

from lib.log import get_logger
from lib.tool_call_identity import ensure_unique_tool_call_ids
from lib.tool_caller_identity import (
    MAX_TOOL_CALLER_ID_CHARS,
    normalize_tool_caller,
    tool_caller_authority,
)
from lib.tool_round_replay import SUPERSEDED_PROVIDER_ATTEMPT_FIELD
from lib.tools.contracts import (
    ToolContractError,
    validate_tool_arguments_from_documents,
)

logger = get_logger(__name__)


class _ContentWithDisplayResults(str):
    """String subclass that carries display_results metadata.

    Used by ``_execute_one`` for web_search to pass both the formatted
    LLM content (as a string) and the display results for the frontend,
    through the existing cache pipeline that expects string content.

    Attributes:
        display_results: List of result dicts for frontend rendering.
        search_diag: Optional diagnostic dict when search returns 0 results.
        engine_breakdown: Optional dict mapping engine tag → list of raw URLs.

    ``display_results`` MUST be optional: ``str`` subclasses are reconstructed
    by copy/deepcopy/pickle via ``cls.__new__(cls, <str value>)`` — a single
    positional arg (``str.__getnewargs__`` returns a 1-tuple). If it were
    required, ``copy.deepcopy(body['messages'])`` in the dispatch path
    (lib/llm_dispatch/api.py) would raise ``__new__() missing 1 required
    positional argument: 'display_results'`` the moment a web_search/fetch_url
    result reaches the model call, killing the whole turn. deepcopy/pickle
    restore the instance ``__dict__`` after ``__new__``, so the real metadata
    is preserved regardless of this default.
    """
    def __new__(cls, content: str, display_results: list | None = None,
                *, cacheable: bool = True):
        instance = super().__new__(cls, content)
        instance.display_results = display_results if display_results is not None else []
        instance.search_diag = None
        instance.engine_breakdown = None
        instance.vertical = None
        # Outcome policy, independent of the tool's contract-level
        # idempotency. Transient failures must fall through to the ordinary
        # handler instead of becoming an authoritative prefetch hit.
        instance.cacheable = bool(cacheable)
        return instance


class _ContentWithResultProjection(str):
    """String result carrying bounded, request-local result sidecars.

    Batched ``read_files`` still returns its legacy plain-text representation,
    but the V2 result budget needs the producer's per-file boundaries to avoid
    spending the entire preview budget on the first file.  A ``str`` subclass
    keeps every existing read/freshness caller compatible while transporting
    the sidecar through the streaming future into the dedup cache.
    """

    def __new__(cls, content: str, projection_items: list | None = None,
                *, producer_metadata: dict | None = None):
        instance = super().__new__(cls, content)
        instance.result_projection_items = (
            projection_items if projection_items is not None else [])
        instance.result_producer_metadata = producer_metadata
        return instance


# ── Read-only tools safe to pre-execute during streaming ──
# These must have NO side effects (idempotent) and be concurrency-safe.
_STREAMABLE_TOOLS = frozenset({
    'read_files', 'grep_search', 'find_files', 'list_dir',
    'web_search', 'fetch_url',
})
def _stream_prefetch_worker_limit(
    environment: Mapping[str, str] | None = None,
) -> int:
    """Reuse the launch-probed tool budget, retaining the historical hard 4."""
    from runtime_guards import resolve_resource_budget
    return min(4, resolve_resource_budget(
        'TOOL_MAX_PARALLEL_WORKERS',
        environment,
        minimum=1,
        maximum=32,
    ))


_stream_prefetch_workers_cache: int | None = None


def _stream_prefetch_workers() -> int:
    """Return the prefetch pool size, resolved lazily on first use.

    The launch probe was previously evaluated at import time, so a probe
    failure could raise while importing an unrelated module and the value was
    frozen before any operator override could be read. Resolve once at first
    use instead; a probe failure falls back to the lean single-worker floor (a
    speculative latency optimization must never take down streaming).
    """
    global _stream_prefetch_workers_cache
    if _stream_prefetch_workers_cache is None:
        try:
            _stream_prefetch_workers_cache = _stream_prefetch_worker_limit()
        except Exception as error:
            logger.warning(
                '[StreamingToolExec] prefetch worker probe failed; '
                'falling back to 1 worker: %s', error)
            _stream_prefetch_workers_cache = 1
    return _stream_prefetch_workers_cache


# Speculative prefetch ceiling: at most 8 read-only calls per stream are
# submitted early. Calls beyond this still execute through ordinary post-stream
# dispatch, so the bound never drops a model occurrence.
_MAX_STREAM_PREFETCH_CALLS = 8

# ── Internal tool prefixes to skip (proxy artifacts, not real tools) ──
_INTERNAL_TOOL_PREFIXES = ('antml:', 'anthropic.', '__')


def _stream_occurrence_signature(tool_call: dict) -> tuple | None:
    """Return a strict identity for one callback-visible response position.

    A provider retry is a new response attempt even when it recycles the same
    correlation id.  Within one attempt, the signature lets us distinguish an
    exact duplicate callback from a second, conflicting position before the
    ordinary dispatch layer gets a chance to remint duplicate ids.
    """
    if not isinstance(tool_call, dict):
        return None
    function = tool_call.get('function')
    if not isinstance(function, dict):
        return None
    name = function.get('name')
    arguments = function.get('arguments')
    if not isinstance(name, str) or not name or not isinstance(arguments, str):
        return None
    try:
        decoded_arguments = json.loads(arguments) if arguments.strip() else {}
        canonical_arguments = json.dumps(
            decoded_arguments, ensure_ascii=False, sort_keys=True,
            separators=(',', ':'), default=str)
    except (json.JSONDecodeError, TypeError, ValueError):
        canonical_arguments = f'raw:{arguments}'

    caller, caller_error = normalize_tool_caller(
        tool_call.get('caller') if 'caller' in tool_call else None,
        require_program_identity=False,
    )
    if caller_error:
        return None
    authority = (tool_caller_authority(caller)
                 if caller is not None else ('root', ''))
    return name, canonical_arguments, authority


def _has_executable_target(fn_name: str, fn_args: dict) -> bool:
    """True if a streamable read-only call has a usable target to pre-execute.

    Guards against phantom/placeholder calls the model sometimes emits, e.g.
    ``fetch_url({"reason": "placeholder", "urls": []})`` — an empty ``urls``
    array is falsy so it falls through to single-URL mode with ``url=''``.
    Pre-executing that would run ``fetch_page_content('')`` and CACHE a bogus
    ``"Failed to fetch ."`` result (tagged ``source: "Prefetch"``) that the
    real handler then never gets a chance to reject cleanly. Returning False
    here defers the call to the normal handler, which rejects it with a clear
    "no URL provided" message instead.
    """
    if fn_name == 'fetch_url':
        urls = fn_args.get('urls')
        if isinstance(urls, list) and any(
            (isinstance(s, dict) and s.get('url')) or (isinstance(s, str) and s.strip())
            for s in urls
        ):
            return True
        url = fn_args.get('url')
        return isinstance(url, str) and bool(url.strip())
    if fn_name == 'web_search':
        queries = fn_args.get('queries')
        if isinstance(queries, list) and any(
            (isinstance(s, dict) and s.get('query')) or (isinstance(s, str) and s.strip())
            for s in queries
        ):
            return True
        query = fn_args.get('query')
        return isinstance(query, str) and bool(query.strip())
    # Project tools (read_files, grep_search, …) — let the handler validate.
    return True


class StreamingToolAccumulator:
    """Accumulates tool calls during streaming and pre-executes read-only ones.

    Also emits ``tool_start`` SSE events immediately as each tool call is
    parsed from the stream, so the frontend shows the tool status without
    waiting for the entire LLM response to finish.

    Usage::

        acc = StreamingToolAccumulator(
            task, project_path,
            tool_round_num=tool_round_num,
            round_num=round_num,
            project_enabled=project_enabled,
        )
        msg, finish, usage = stream_llm_response(
            task, body, tag='R1',
            on_tool_call_ready=acc.on_tool_call_ready,
        )
        # Read back the updated tool_round_num
        tool_round_num = acc.tool_round_num
        # Inject completed results into dedup cache
        hit_count = acc.inject_into_cache(task)
        # Now parse_tool_calls will skip re-emitting for already-announced tools
        parsed_tcs, tool_round_num = parse_tool_calls(
            assistant_msg, task, round_num, tool_round_num, project_enabled,
            early_announced=acc.announced_tc_map,
        )

    Args:
        task: Live task dict.
        project_path: Base path for project tools (may be None).
        tool_round_num: Current tool round counter (will be incremented).
        round_num: Current orchestrator loop round (for llmRound tagging).
        project_enabled: Whether project-mode is active.
    """

    def __init__(self, task: dict, project_path: str | None,
                 tool_round_num: int = 0, round_num: int = 0,
                 project_enabled: bool = False):
        self._task = task
        self._project_path = project_path
        self._tool_round_num = tool_round_num
        self._round_num = round_num
        self._project_enabled = project_enabled
        self._pool = ThreadPoolExecutor(max_workers=_stream_prefetch_workers(),
                                        thread_name_prefix='stream-tool')
        self._closed = False
        # tc_id → (future, fn_name, fn_args, submit_time)
        self._futures: dict[str, tuple[Future, str, dict, float]] = {}
        self._submitted_count = 0
        self._prefetch_limit_logged = False
        self._tid = task['id'][:8]
        # tc_id → (rn, round_entry) for tools already announced via tool_start
        self._announced: dict[str, tuple[int, dict]] = {}
        self._announced_signatures: dict[str, tuple] = {}
        # Rows/futures from a provider attempt that was explicitly discarded
        # must remain visible long enough to receive a terminal verdict, but
        # they are never candidates for the adopted response or its cache.
        self._discarded_announced: list[tuple[str, int, dict]] = []
        self._discarded_futures: list[Future] = []
        self._claimed_callback_ids: set[str] = set()
        self._announced_count = 0
        self._first_announced = True  # for assistantContent tagging

    @property
    def tool_round_num(self) -> int:
        """Current tool_round_num (updated as tools are announced)."""
        return self._tool_round_num

    @property
    def announced_tc_map(self) -> dict[str, tuple[int, dict]]:
        """Active adopted-attempt rows eligible for parser reconciliation."""
        return dict(self._announced)

    @property
    def announced_count(self) -> int:
        """Total rows announced, including rows from discarded attempts."""
        return self._announced_count

    def close(self, *, cancel_futures: bool = True,
              wait: bool = False) -> None:
        """Idempotently release speculative work on every round exit path."""
        if self._closed:
            return
        self._closed = True
        if cancel_futures:
            for future, _name, _args, _started in self._futures.values():
                future.cancel()
            for future in self._discarded_futures:
                future.cancel()
        try:
            self._pool.shutdown(
                wait=wait, cancel_futures=cancel_futures)
        except Exception as error:
            logger.warning(
                '[%s] StreamingToolExec: pool shutdown failed: %s',
                self._tid, error)
        finally:
            self._futures.clear()
            self._discarded_futures.clear()

    def on_provider_attempt_restart(self, *, reason: str = '') -> None:
        """Retire all early work owned by a discarded provider attempt.

        The retry callback fires before the replacement request starts.  Keep
        its rows for an explicit ``aborted`` settlement, clear them from the
        parser's adoption map, and quarantine its read-only futures so their
        results can never satisfy the replacement response by content alone.
        """
        if not self._announced and not self._futures:
            return
        for tc_id, (round_num, round_entry) in self._announced.items():
            self._discarded_announced.append((tc_id, round_num, round_entry))
        self._announced.clear()
        self._announced_signatures.clear()
        for future, _name, _args, _started in self._futures.values():
            future.cancel()
            self._discarded_futures.append(future)
        self._futures.clear()
        logger.info(
            '[%s] StreamingToolExec: retired discarded provider attempt '
            '(rows=%d futures=%d reason=%s)',
            self._tid, len(self._discarded_announced),
            len(self._discarded_futures), reason or 'retry')

    def reconcile_announced_rounds(self, assistant_msg: dict) -> int:
        """Settle orphan early-announced rounds whose tc_id was superseded.

        ``on_tool_call_ready`` fires as each tool call's arguments finish
        streaming; ``_emit_tool_start`` immediately appends a
        ``status='searching'`` round to ``task['toolRounds']`` and emits a
        ``tool_start`` (the live spinner). A round is ORPHANED when its
        ``tc_id`` is NOT present in the FINAL adopted ``assistant_msg`` — the
        dispatch pipeline only settles ids that ARE in that message, so an
        orphan would spin at ``searching`` forever (live AND after reload,
        since it persists into ``conversations.messages``).

        TWO distinct upstream causes produce such an orphan — this method
        handles both, but they are NOT equally common (verified against
        production ``logs/app.log``):

          1. **FloorRetry adoption (the dominant, effectively sole cause).**
             ``stream_llm_response`` resends the IDENTICAL body on a byte-stable
             cache floor-collapse (``TOFU_CACHE_FLOOR_RETRY``, default ON) and
             ADOPTS the recovered resend's response. The resend re-mints tool
             calls under FRESH ``tc_id``s, so the first attempt's already-
             announced rounds are not in the adopted message → orphaned. The
             resend itself no longer announces (Layer-1 passes
             ``on_tool_call_ready=None``), so the orphan is always the FIRST
             attempt's round. ``task['_floor_retry_adopted']`` flags this.
          2. **Transient stream retry.** ``stream_chat`` transparently re-runs
             the SSE stream up to ``MAX_STREAM_RETRIES`` on a mid-stream error
             while reusing the SAME callback; an early attempt that fired the
             callback leaves a fresh-``tc_id`` orphan. In production this path
             fired ZERO times over the sampled window (all observed orphans
             were cause #1) — the original docstring/log wrongly blamed THIS
             cause exclusively, which is why the symptom was repeatedly
             mis-traced across sessions.

        This runs once, right after the LLM call returns, BEFORE
        ``parse_tool_calls``: for every orphaned announced round it stamps a
        terminal ``aborted`` status and emits a ``tool_result``-class event (via
        ``_finalize_tool_round``, ``badge='superseded'``) so the live stream and
        the persisted/reloaded DB state agree — the same discipline as the
        task-end dangling sweep (``orchestrator._finalize_dangling_tool_rounds``),
        applied at the per-round seam where the orphan is created. The husk is
        DROPPED from both render paths and from model-context reconstruction
        (``_isSupersededOrphanRound`` / ``_is_reconstructable_round``), so it is
        never shown nor sent to the model — it exists only to flip the spinner.

        Returns the number of orphan rounds finalized.
        """
        if not self._announced and not self._discarded_announced:
            return 0
        # True cause of any orphan this call settles (see docstring): FloorRetry
        # adoption re-minted tc_ids, vs a transient stream retry. Read the marker
        # stream_llm_response stamps; default to the retry story only when the
        # marker is absent (older call sites / tests).
        _fr_adopted = bool(self._task.get('_floor_retry_adopted'))
        _cause = ('a FloorRetry resend adoption (identical-body cache-floor '
                  'recovery re-minted tc_ids)' if _fr_adopted
                  else 'a discarded stream-retry attempt')
        # A recovered FloorRetry response is dispatched with the callback
        # disabled.  If it is adopted, every row/future announced by the
        # original response is non-authoritative even when the resend happens
        # to recycle the same call id.
        if _fr_adopted:
            self.on_provider_attempt_restart(reason='floor-retry adoption')

        final_occurrences: dict[str, list[tuple]] = {}
        raw_final_calls = (
            assistant_msg.get('tool_calls')
            if isinstance(assistant_msg, dict) else None)
        for tool_call in raw_final_calls if isinstance(raw_final_calls, list) else []:
            if not isinstance(tool_call, dict):
                continue
            call_id = tool_call.get('id')
            signature = _stream_occurrence_signature(tool_call)
            if (isinstance(call_id, str) and call_id and signature is not None):
                final_occurrences.setdefault(call_id, []).append(signature)

        orphans = list(self._discarded_announced)
        self._discarded_announced.clear()
        for tc_id, (rn, entry) in list(self._announced.items()):
            signature = self._announced_signatures.get(tc_id)
            candidates = final_occurrences.get(tc_id) or []
            if signature in candidates:
                candidates.remove(signature)
                continue
            orphans.append((tc_id, rn, entry))
            self._announced.pop(tc_id, None)
            self._announced_signatures.pop(tc_id, None)
            discarded_future = self._futures.pop(tc_id, None)
            if discarded_future is not None:
                future = discarded_future[0]
                future.cancel()
                self._discarded_futures.append(future)
        if not orphans:
            return 0

        from lib.tasks_pkg.executor import _finalize_tool_round

        finalized = 0
        for tc_id, rn, entry in orphans:
            if not isinstance(entry, dict):
                continue
            # Only settle a still-unsettled round — never overwrite a real
            # result (defensive: an orphan should never carry one).
            if entry.get('status') not in (None, 'searching', 'executing') \
                    or entry.get('results'):
                continue
            tool_name = entry.get('toolName') or 'tool'
            query = entry.get('query') or tool_name
            _snippet = ('Superseded — an identical-body resend recovered a '
                        'cheaper cache read and its response was adopted, so '
                        'this earlier call was dropped.' if _fr_adopted
                        else 'Superseded — the stream reconnected and re-issued '
                        'this call.')
            meta = {
                'toolName': tool_name,
                'title': query,
                'snippet': _snippet,
                'source': 'Interrupted',
                'fetched': False,
                'fetchedChars': 0,
                'badge': 'superseded',
                'interrupted': True,
            }
            # This row is a discarded transport-attempt announcement, not a
            # tool execution.  Stamp the semantic marker before settlement so
            # both the fallback path and every persisted projection retain it.
            entry[SUPERSEDED_PROVIDER_ATTEMPT_FIELD] = True
            try:
                # The verdict goes THROUGH the seam so the emitted tool_result
                # frame and the persisted round agree — a post-hoc downgrade
                # would ship a 'done' frame for an 'aborted' round.
                _finalize_tool_round(
                    self._task, rn, entry, [meta],
                    query_override=query,
                    status='aborted',
                    extra_event_fields={
                        SUPERSEDED_PROVIDER_ATTEMPT_FIELD: True,
                    },
                )
                finalized += 1
                logger.info(
                    '[%s] StreamingToolExec: settled orphan early-announced '
                    'round %s (tool=%s tc_id=%s) — superseded by %s',
                    self._tid, rn, tool_name, tc_id[:8], _cause)
            except Exception as e:
                entry['status'] = 'aborted'
                finalized += 1
                logger.warning(
                    '[%s] StreamingToolExec: _finalize_tool_round failed for '
                    'orphan round %s (tool=%s): %s — status stamped aborted anyway',
                    self._tid, rn, tool_name, e, exc_info=True)
        if finalized:
            logger.info('[%s] StreamingToolExec: reconciled %d orphan '
                        'early-announced round(s) — cause=%s',
                        self._tid, finalized,
                        'floor_retry_adoption' if _fr_adopted else 'stream_retry')
            try:
                from lib.log import audit_log
                audit_log('tool_round_superseded',
                          task_id=self._task.get('id', '') or '',
                          conv_id=self._task.get('convId', '') or '',
                          count=finalized,
                          cause=('floor_retry_adoption' if _fr_adopted
                                 else 'stream_retry'))
            except Exception as _ae:
                logger.debug('[%s] audit_log(tool_round_superseded) failed: %s',
                             self._tid, _ae)
        return finalized

    def on_tool_call_ready(self, tool_call: dict):
        """Callback fired when a tool call's arguments finish streaming.

        Called from ``_stream_chat_once`` in the SSE delta processing loop.

        1. Emits a ``tool_start`` SSE event for ALL tools immediately
           (so the frontend shows "Searching…" / "Running…" right away).
        2. Submits read-only, concurrency-safe tools for pre-execution.
        """
        if not isinstance(tool_call, dict):
            return
        if self._closed:
            return
        function = tool_call.get('function')
        if not isinstance(function, dict):
            return
        fn_name = function.get('name', '')
        tc_id = tool_call.get('id', '')
        fn_args_raw = function.get('arguments', '')

        if (not isinstance(fn_name, str) or not isinstance(tc_id, str)
                or not isinstance(fn_args_raw, str)
                or not fn_name or not tc_id
                or len(fn_name) > 512
                or len(tc_id) > MAX_TOOL_CALLER_ID_CHARS):
            return

        # Skip internal/spurious tool names (proxy artifacts)
        if any(fn_name.startswith(p) for p in _INTERNAL_TOOL_PREFIXES):
            return

        # Don't announce if task is aborted
        if self._task.get('aborted'):
            return

        # A program item and its children arrive in the same Responses stream.
        # Defer program-issued calls until post-stream reconciliation so the
        # canonical program parent + hard call budget exist before any child
        # is announced or executed. Direct calls keep the latency prefetch.
        caller, caller_error = normalize_tool_caller(
            tool_call.get('caller'), require_program_identity=False)
        caller_type = caller.get('type') if caller is not None else ''
        if ('caller' in tool_call and tool_call.get('caller') is not None
                and caller_error is not None):
            logger.warning(
                '[%s] StreamingToolExec: deferring invalid attributed call '
                '%s (tc_id=%s) to the authority validator',
                self._tid, fn_name, tc_id[:8])
            return
        if caller is not None:
            tool_call['caller'] = caller
        if caller_type == 'program':
            logger.debug(
                '[%s] StreamingToolExec: deferring program child %s '
                '(tc_id=%s caller=%s) until parent reconciliation',
                self._tid, fn_name, tc_id[:8], caller.get('caller_id', ''))
            return

        # Do not filter empty-args calls here. A distinct provider occurrence
        # may be a legitimate no-arg call; otherwise the request-owned schema
        # validator returns its typed rejection after streaming. Similarity to
        # a same-named sibling is not evidence that either occurrence is fake.

        # ── Parse arguments ──
        try:
            fn_args = json.loads(fn_args_raw) if fn_args_raw.strip() else {}
        except (json.JSONDecodeError, TypeError) as _e_audit:
            # Can't parse → still emit tool_start with empty args for UI feedback
            logger.debug('[streaming_tool_executor] on_tool_call_ready caught %s: %s', type(_e_audit).__name__, _e_audit)
            fn_args = {}
        if not isinstance(fn_args, dict):
            logger.warning(
                '[%s] StreamingToolExec: deferring non-object arguments for '
                '%s (tc_id=%s) to the typed parser',
                self._tid, fn_name, tc_id[:8])
            fn_args = {}
            contract_allows_prefetch = False
        else:
            contract_allows_prefetch = True

        # A read-only prefetch still executes real code. Validate it against
        # the same request-owned contract before submitting the future; the
        # post-stream parser will render the typed rejection. ``None`` keeps
        # old standalone/test callers read-compatible, while a present map
        # (including an empty one) fails closed on schema drift.
        try:
            if contract_allows_prefetch:
                fn_args = validate_tool_arguments_from_documents(
                    (self._task.get('_toolContractDocumentsByName')
                     if '_toolContractDocumentsByName' in self._task else None),
                    fn_name, fn_args)
        except ToolContractError as exc:
            contract_allows_prefetch = False
            logger.warning(
                '[%s] StreamingToolExec: contract rejected pre-execution '
                'tool=%s code=%s path=%s',
                self._tid, fn_name, exc.code, exc.path)

        occurrence_signature = _stream_occurrence_signature(tool_call)
        if tc_id in self._announced:
            if self._announced_signatures.get(tc_id) == occurrence_signature:
                # This may be a retransmitted callback or a distinct exact twin.
                # One early row/prefetch is enough for latency; the final parser
                # still retains and independently settles every response position.
                logger.warning(
                    '[%s] StreamingToolExec: duplicate callback call_id=%s '
                    'ignored before announcement/prefetch',
                    self._tid, tc_id[:8])
                return
            # Same id, different action in one response: repair the callback
            # object itself. SSE finalization retains this object, so the final
            # assistant protocol and early row share the fresh identity.
            ensure_unique_tool_call_ids(
                [tool_call], self._claimed_callback_ids,
                id_prefix=f'stream_r{self._round_num}')
            tc_id = tool_call['id']
            logger.warning(
                '[%s] StreamingToolExec: conflicting callback call_id reminted '
                'before announcement (tool=%s id=%s)',
                self._tid, fn_name, tc_id[:12])
        elif tc_id in self._claimed_callback_ids:
            # The same provider id came back after an explicit attempt restart.
            # It is a new response occurrence even when payload bytes match.
            ensure_unique_tool_call_ids(
                [tool_call], self._claimed_callback_ids,
                id_prefix=f'stream_retry_r{self._round_num}')
            tc_id = tool_call['id']
        else:
            self._claimed_callback_ids.add(tc_id)

        # ── Emit tool_start SSE event immediately ──
        try:
            self._emit_tool_start(
                fn_name, fn_args, tc_id,
                json.dumps(fn_args, ensure_ascii=False, separators=(',', ':')),
                caller=tool_call.get('caller'),
                signature_arguments=fn_args_raw)
        except Exception as e:
            logger.debug('[%s] StreamingToolExec: tool_start emission failed '
                         'for %s: %s', self._tid, fn_name, e)

        # ── Pre-execute read-only tools ──
        if (contract_allows_prefetch and fn_name in _STREAMABLE_TOOLS and fn_args
                and _has_executable_target(fn_name, fn_args)
                and self._submitted_count < _MAX_STREAM_PREFETCH_CALLS):
            self._submitted_count += 1
            t0 = time.time()
            logger.info('[%s] StreamingToolExec: pre-executing %s (tc_id=%s) '
                        'while model streams',
                        self._tid, fn_name, tc_id[:8])

            try:
                future = self._pool.submit(
                    self._execute_one, fn_name, fn_args
                )
            except Exception as error:
                self._submitted_count -= 1
                # Speculative prefetch is an observer/latency optimization.
                # Pool shutdown/saturation must not escape into the provider
                # SSE callback and terminate an otherwise healthy model stream;
                # the ordinary post-stream tool path will execute this call.
                logger.warning(
                    '[%s] StreamingToolExec: prefetch submit failed for %s '
                    '(tc_id=%s); deferring to post-stream execution: %s',
                    self._tid, fn_name, tc_id[:8], error,
                )
                return
            self._futures[tc_id] = (future, fn_name, fn_args, t0)
        elif (contract_allows_prefetch and fn_name in _STREAMABLE_TOOLS
              and fn_args and _has_executable_target(fn_name, fn_args)
              and not self._prefetch_limit_logged):
            self._prefetch_limit_logged = True
            logger.warning(
                '[%s] StreamingToolExec: speculative queue reached %d calls; '
                'remaining occurrences will execute through normal dispatch',
                self._tid, _MAX_STREAM_PREFETCH_CALLS)

    def _emit_tool_start(self, fn_name: str, fn_args: dict, tc_id: str,
                         tc_args_str: str, caller=None,
                         signature_arguments: str | None = None):
        """Emit a tool_start SSE event + append round entry to task.

        Uses the same ``_build_tool_round_entry`` as ``parse_tool_calls``
        to ensure consistent roundNum assignment and display formatting.

        Requires ``task['toolRounds']`` and ``task['events_lock']`` to exist.
        Silently skips if the task doesn't have these (e.g. in unit tests).
        """
        # Guard: skip if task is not fully initialised (e.g. unit tests)
        if 'toolRounds' not in self._task:
            return

        from lib.tasks_pkg.manager import append_event
        from lib.tasks_pkg.tool_display import _build_tool_round_entry

        self._tool_round_num, round_entry, event_payload = _build_tool_round_entry(
            fn_name, fn_args, tc_id, tc_args_str,
            self._tool_round_num, self._project_enabled,
            conv_id=self._task.get('convId') or self._task.get('id'),
            task=self._task,
        )
        rn = round_entry['roundNum']

        # Tag with LLM round (same as parse_tool_calls does)
        round_entry['llmRound'] = self._round_num
        event_payload['llmRound'] = self._round_num
        if isinstance(caller, dict):
            round_entry['caller'] = dict(caller)
            event_payload['caller'] = dict(caller)
            if caller.get('type') == 'program' and caller.get('caller_id'):
                round_entry['_programCallId'] = caller['caller_id']
                event_payload['programCallId'] = caller['caller_id']

        # Append to task's toolRounds and emit SSE event
        self._task['toolRounds'].append(round_entry)
        append_event(self._task, event_payload)

        # Track as announced
        self._announced[tc_id] = (rn, round_entry)
        # The announced identity must derive from the SAME wire bytes the
        # final assembled assistant message carries. ``tc_args_str`` is the
        # validated serialization, and the contract validator fills schema
        # defaults (contracts._validate) — keying on it orphans every call
        # whose model omitted a defaulted arg once reconcile matches against
        # the raw final ``function.arguments``.
        signature_call = {
            'id': tc_id,
            'function': {
                'name': fn_name,
                'arguments': (signature_arguments
                              if isinstance(signature_arguments, str)
                              else tc_args_str),
            },
        }
        if caller is not None:
            signature_call['caller'] = caller
        signature = _stream_occurrence_signature(signature_call)
        if signature is not None:
            self._announced_signatures[tc_id] = signature
        self._announced_count += 1

        logger.info('[%s] StreamingToolExec: early tool_start emitted for '
                    '%s (tc_id=%s, rn=%d) — UI shows activity immediately',
                    self._tid, fn_name, tc_id[:8], rn)

    def _execute_one(self, fn_name: str, fn_args: dict) -> str:
        """Execute a single read-only tool call in a background thread.

        Uses the same underlying tool functions as the normal pipeline
        but without the event/round_entry overhead.

        Returns:
            Tool result content as string.
        """
        # Abort check: skip execution if user already clicked Stop
        if self._task.get('aborted'):
            logger.info('[%s] StreamingToolExec: skipping %s — task aborted',
                        self._tid, fn_name)
            return _ContentWithDisplayResults(
                'Task aborted by user.', cacheable=False)

        try:
            if fn_name in ('read_files', 'grep_search', 'find_files',
                           'list_dir'):
                from lib.project_mod.tools import execute_tool
                # Pass conv_id so namespaced paths resolve against this
                #   conversation's root registry (prevents concurrent-task
                #   clobber — see lib/project_mod/config.py::set_conv_roots).
                _conv_id = self._task.get('convId') or self._task.get('id') or ''
                _base = self._project_path or '.'
                _projection_items = [] if fn_name == 'read_files' else None
                _execute_kwargs = {}
                if _projection_items is not None:
                    _execute_kwargs['result_projection_items'] = _projection_items
                _content = execute_tool(
                    fn_name, fn_args, _base, conv_id=_conv_id,
                    **_execute_kwargs)
                # Write-freshness token — THIS pre-exec path bypasses
                #   _handle_project_tool (its result is cached as authoritative
                #   and the serial pipeline skips re-execution), so the
                #   handler's record_read_paths never runs for streamed reads.
                #   Without the stamp here, a conversation that READ a file is
                #   unprotected on its next write (fail-open clobber), and a
                #   refused write can NEVER recover — the instructed re-read
                #   lands here and refreshes nothing (production refusal loop,
                #   2026-07-25: repeated 'stale' refusals after each re-read).
                if fn_name == 'read_files':
                    try:
                        from lib.tasks_pkg.handlers._write_freshness_gate import (
                            record_read_paths,
                        )
                        record_read_paths(self._task, fn_args,
                                          self._project_path, _content)
                    except Exception as _fe:
                        logger.debug('[%s] StreamingToolExec: freshness '
                                     'read-token record failed (non-fatal): %s',
                                     self._tid, _fe)
                _producer_metadata = None
                if fn_name == 'grep_search' and isinstance(_content, str):
                    from lib.project_mod.read_tools import (
                        grep_result_was_truncated,
                    )
                    if grep_result_was_truncated(_content):
                        _producer_metadata = {
                            'status': 'partial', 'truncated': True}
                if (isinstance(_content, str)
                        and (_projection_items or _producer_metadata)):
                    _content = _ContentWithResultProjection(
                        _content, _projection_items,
                        producer_metadata=_producer_metadata)
                return _content

            elif fn_name == 'web_search':
                # Delegate to the SINGLE SOURCE OF TRUTH for search —
                # handlers.search._web_search_one — so the streaming pre-exec
                # path is byte-identical to the serial handler: the vertical
                # thread-pool is shut down (the old inline copy LEAKED a pool
                # per vertical query), perform_web_search is wrapped in the
                # same try/except safety net (graceful search_diag on failure
                # instead of an escaping exception / raw error string), and the
                # vertical-timeout is the same. Same authoritative-cache
                # hazard as the fetch_url fix: this result is cached and the
                # serial pipeline SKIPS re-execution, so any drift here was
                # silently served. (Lazy import avoids a cycle.)
                from lib.tasks_pkg.handlers.search._core import _web_search_one
                from lib.tasks_pkg.handlers.search._display import (
                    _format_search_display_for_results,
                    _vertical_header_for_llm,
                    _vertical_to_sse_payload,
                )
                from tofu_search.search import format_search_for_tool_response
                user_question = self._task.get('lastUserQuery', '')
                from lib.search_bridge import bind_search_browser
                owner_user_id = self._task.get('_userId', '') or ''
                configured_client_id = (
                    (self._task.get('config') or {}).get('browserClientId') or '')

                # Resolve an unselected browser once, then bind that exact
                # owner/device inside every worker thread. ContextVars do not
                # propagate through ThreadPoolExecutor on their own.
                with bind_search_browser(
                        user_id=owner_user_id,
                        client_id=configured_client_id) as selected_binding:
                    selected_client_id = selected_binding[1]

                # Batch mode: run concurrent searches (lightweight, no SSE events)
                # Parity with serial _handle_web_search_batch (handlers/search.py):
                # same (query, freshness, vertical) specs, run_batch_concurrent
                # orchestration, per-query `_q` tagging, and {'batch': [...]}
                # vertical carrier (consumed at tool_dispatch.py:1063).
                queries = fn_args.get('queries')
                batch_vertical = fn_args.get('vertical', 'auto')
                if queries and isinstance(queries, list):
                    from lib.tasks_pkg.handlers._adapter import run_batch_concurrent
                    batch_freshness = fn_args.get('freshness', '')
                    specs = []
                    for s in queries[:5]:
                        if isinstance(s, dict) and s.get('query'):
                            specs.append((s['query'],
                                          s.get('freshness', '') or batch_freshness,
                                          s.get('vertical') or batch_vertical))
                        elif isinstance(s, str) and s.strip():
                            specs.append((s.strip(), batch_freshness, batch_vertical))

                    def _worker(spec):
                        q, f, v = spec
                        with bind_search_browser(
                                user_id=owner_user_id,
                                client_id=selected_client_id):
                            results, search_diag, _bkdn, vertical_result = _web_search_one(
                                q, user_question, f, vertical=v)
                        fmt = format_search_for_tool_response(results, search_diag=search_diag, query=q)
                        if vertical_result:
                            fmt = _vertical_header_for_llm(vertical_result) + fmt
                        return (results, fmt, vertical_result)

                    ordered = run_batch_concurrent(specs, _worker, max_workers=5, tag='Search')
                    n = len(specs)
                    all_display_results = []
                    verticals = []
                    parts = []
                    for idx, item in enumerate(ordered):
                        q = specs[idx][0]
                        if item is None:
                            parts.append(f'Search failed for "{q}": internal error (see logs)')
                            continue
                        results, fmt, vertical_result = item
                        disp = _format_search_display_for_results(results)
                        for dr in disp:
                            dr['_q'] = q
                        all_display_results.extend(disp)
                        if vertical_result:
                            payload = _vertical_to_sse_payload(vertical_result)
                            if payload:
                                payload = dict(payload)
                                payload['query'] = q
                                verticals.append(payload)
                        parts.append(f'=== Search: {q} ===\n{fmt}' if n > 1 else fmt)
                    formatted = _ContentWithDisplayResults(
                        '\n\n'.join(p for p in parts if p),
                        all_display_results,
                    )
                    if verticals:
                        formatted.vertical = {'batch': verticals}
                    return formatted

                # ── Single query ──
                query = fn_args.get('query', '')
                vertical_param = fn_args.get('vertical', 'auto')
                freshness = fn_args.get('freshness', '')
                with bind_search_browser(
                        user_id=owner_user_id,
                        client_id=selected_client_id):
                    results, search_diag, engine_breakdown, vertical_result = _web_search_one(
                        query, user_question, freshness, vertical=vertical_param)
                formatted_text = format_search_for_tool_response(results, search_diag=search_diag, query=query)
                if vertical_result:
                    formatted_text = _vertical_header_for_llm(vertical_result) + formatted_text
                display_results = _format_search_display_for_results(results)
                formatted = _ContentWithDisplayResults(formatted_text, display_results)
                if not display_results and search_diag:
                    formatted.search_diag = search_diag
                if engine_breakdown:
                    formatted.engine_breakdown = engine_breakdown
                vertical_payload = _vertical_to_sse_payload(vertical_result)
                if vertical_payload:
                    formatted.vertical = vertical_payload
                return formatted

            elif fn_name == 'fetch_url':
                # Delegate to the SINGLE SOURCE OF TRUTH for URL fetching —
                # handlers.search._fetch_url_one — so the streaming pre-exec
                # path handles binary file assets (staged to data/fetched/ for
                # read_files) and text assets (SVG/JSON/source returned raw,
                # skipping the article filter) IDENTICALLY to the serial
                # pipeline. Using the old text-only fetch_page_content here
                # silently returned nothing for those URLs, and because this
                # result is injected into _tool_result_cache as authoritative,
                # the serial pipeline then SKIPPED re-execution — so the loss
                # was invisible. (Lazy import avoids a cycle: search.py imports
                # from executor, which streaming_tool_executor also uses.)
                from lib.tasks_pkg.handlers.search._core import _fetch_url_one
                from lib.tasks_pkg.handlers.search._display import _format_fetch_display
                from lib.tasks_pkg.tool_display import _short_url
                user_question = self._task.get('lastUserQuery', '')
                from lib.search_bridge import bind_search_browser
                owner_user_id = self._task.get('_userId', '') or ''
                configured_client_id = (
                    (self._task.get('config') or {}).get('browserClientId') or '')
                with bind_search_browser(
                        user_id=owner_user_id,
                        client_id=configured_client_id) as selected_binding:
                    selected_client_id = selected_binding[1]

                # Batch mode: run concurrent fetches (lightweight, no SSE events)
                # Parity with the serial batch worker (handlers/search.py:614) —
                # it passes fetch_reason='' for batch URLs (no per-URL reason).
                urls = fn_args.get('urls')
                if urls and isinstance(urls, list):
                    from concurrent.futures import ThreadPoolExecutor as _TP, as_completed as _ac
                    url_list = [
                        (s.get('url') if isinstance(s, dict) else s)
                        for s in urls[:10]
                        if (isinstance(s, dict) and s.get('url')) or (isinstance(s, str) and s.strip())
                    ]
                    parts = [None] * len(url_list)
                    display_results = [None] * len(url_list)
                    def _worker(target_url):
                        with bind_search_browser(
                                user_id=owner_user_id,
                                client_id=selected_client_id):
                            return _fetch_url_one(
                                target_url, user_question, '')

                    with _TP(max_workers=min(len(url_list), 8)) as pool:
                        futs = {pool.submit(_worker, u): i
                                for i, u in enumerate(url_list)}
                        for f in _ac(futs):
                            idx = futs[f]
                            u = url_list[idx]
                            try:
                                item = f.result()
                            except Exception as e:
                                logger.debug('[%s] StreamingToolExec: batch fetch %r failed: %s',
                                             self._tid, u, e)
                                item = {
                                    'url': u, 'page_content': None, 'is_pdf': False,
                                    'raw_chars': 0, 'filtered_chars': 0,
                                    'error_msg': f'internal fetch error: {str(e)[:120]}',
                                    'saved_path': None, 'is_asset': False,
                                }
                            page_content = item.get('page_content')
                            filtered_chars = item.get('filtered_chars', 0)
                            error_msg = item.get('error_msg')
                            if page_content:
                                parts[idx] = (f"Content from {u} "
                                              f"({filtered_chars:,} chars):\n\n{page_content}")
                            else:
                                parts[idx] = (f"Failed to fetch {u}."
                                              + (f' ({error_msg})' if error_msg else ''))
                            display_results[idx] = _format_fetch_display(item, _short_url)
                    formatted = _ContentWithDisplayResults(
                        '\n\n'.join(p for p in parts if p),
                        [d for d in display_results if d is not None],
                        cacheable=all(
                            part is not None
                            and not part.startswith('Failed to fetch ')
                            for part in parts),
                    )
                    return formatted

                url = fn_args.get('url', '')
                fetch_reason = fn_args.get('reason', '')
                with bind_search_browser(
                        user_id=owner_user_id,
                        client_id=selected_client_id):
                    item = _fetch_url_one(
                        url, user_question, fetch_reason=fetch_reason)
                page_content = item.get('page_content')
                filtered_chars = item.get('filtered_chars', 0)
                error_msg = item.get('error_msg')
                if page_content:
                    return _ContentWithDisplayResults(
                        f"Content from {url} "
                        f"({filtered_chars:,} chars):\n\n{page_content}",
                        [_format_fetch_display(item, _short_url)],
                    )
                return _ContentWithDisplayResults(
                    f"Failed to fetch {url}."
                    + (f' ({error_msg})' if error_msg else ''),
                    [_format_fetch_display(item, _short_url)],
                    cacheable=False,
                )

            return ''

        except Exception as e:
            # UnknownWorkspaceRootError is already logged ONCE as WARNING
            # at the raise site (lib/project_mod/tools.py) and will be
            # re-logged at INFO by executor._execute_tool_one after the
            # normal-pipeline fallback. Keep it at INFO here too so we
            # don't triple-log the same event in error.log.
            try:
                from lib.project_mod.config import UnknownWorkspaceRootError
                if isinstance(e, UnknownWorkspaceRootError):
                    logger.info(
                        '[%s] StreamingToolExec: pre-exec of %s hit '
                        'unknown workspace root (recoverable, returned '
                        'to LLM): %s', self._tid, fn_name, e)
                    raise
            except ImportError as _imp:
                logger.debug('[%s] UnknownWorkspaceRootError import '
                             'failed: %s', self._tid, _imp)
            logger.warning('[%s] StreamingToolExec: pre-exec of %s failed: %s',
                           self._tid, fn_name, e)
            raise

    @staticmethod
    def _normalize_image_result(content):
        """Convert image dict results to __screenshot__ protocol.

        read_files returns ``{'__batch_images__': {idx: screenshot_dict}, '_text_content': ...}``
        for image files.  The handler in ``handlers/project.py`` normally converts
        this to a ``__screenshot__`` dict, but the streaming executor bypasses handlers.

        Without this conversion, ``str(content)`` on the batch dict would dump
        800K+ of base64 text into the cache, which then gets injected as plain text
        into the conversation context (blowing up the token count).

        Returns:
            The original content (if not an image dict), or the extracted
            ``__screenshot__`` dict (preserving _text_fallback).
        """
        if not isinstance(content, dict):
            return content
        # Single __screenshot__ — already in the right format
        if content.get('__screenshot__'):
            return content
        # __batch_images__ — preserve EVERY image (the model and UI both need
        # all of them).  We keep the first image's fields at the top level for
        # backward compatibility with single-image consumers, and add an
        # ``images`` list carrying the full batch so downstream code can emit
        # one image_url block / thumbnail per image.
        if content.get('__batch_images__'):
            images = content['__batch_images__']
            text = content.get('_text_content', '')
            img_list = [v for v in images.values()
                        if isinstance(v, dict) and v.get('__screenshot__')]
            if img_list:
                first_img = dict(img_list[0])
                if text and not first_img.get('_text_fallback'):
                    first_img['_text_fallback'] = text
                if len(img_list) > 1:
                    first_img['images'] = img_list
                return first_img
        return content

    def _prepare_cache_value(self, content, fn_name):
        """Prepare a tool result for cache storage.

        Handles image dicts by preserving them as-is (not stringifying)
        so the post-phase can detect ``__screenshot__`` and convert to
        ``image_url`` blocks instead of dumping base64 as plain text.

        Returns:
            (cache_content, content_len_for_log)
        """
        # Normalize image results from read_files
        content = self._normalize_image_result(content)
        if isinstance(content, dict) and content.get('__screenshot__'):
            # Log compressed size instead of len(dict) which would be key count
            sz = content.get('compressedSize', 0)
            return content, sz
        # Normalize ``str`` subclasses too: their metadata has already been
        # copied into explicit bounded cache slots above, so retaining the
        # subclass would duplicate the sidecar in ``raw_state``.
        content_str = content if type(content) is str else str(content)
        return content_str, len(content_str)

    def inject_into_cache(self, task: dict) -> int:
        """Inject pre-execution results into the dedup cache.

        Waits for ALL submitted futures to complete (with a timeout),
        since these tools would be executed serially by the normal pipeline
        anyway — waiting for already-running work is strictly faster than
        cancelling and re-executing from scratch.

        Returns:
            Count of successfully injected results.
        """
        from lib.tasks_pkg.tool_dispatch._flags import (
            _ensure_tool_result_cache,
            _make_cache_key,
            _store_tool_result_cache_entry,
        )
        _ensure_tool_result_cache(task)

        injected = 0
        # First pass: collect already-done futures immediately
        pending = []
        for tc_id, (future, fn_name, fn_args, t0) in self._futures.items():
            if future.done() and not future.cancelled():
                try:
                    content = future.result(timeout=0)
                    if not getattr(content, 'cacheable', True):
                        logger.info(
                            '[%s] StreamingToolExec: %s pre-exec outcome is '
                            'not cacheable; deferring to normal pipeline',
                            self._tid, fn_name)
                        continue
                    elapsed = time.time() - t0
                    is_search = fn_name in ('web_search',)
                    cache_key = _make_cache_key(fn_name, fn_args)
                    # Extract display_results + engine_breakdown + vertical (web_search)
                    _disp = getattr(content, 'display_results', None)
                    _eng_bkdn = getattr(content, 'engine_breakdown', None)
                    _vert = getattr(content, 'vertical', None)
                    # Zero-result searches carry the diagnostic explaining WHY
                    # (network outage vs no matches) — cache it too, or a
                    # prefetch hit renders a fake single "result" row.
                    _sd = getattr(content, 'search_diag', None)
                    _projection = getattr(
                        content, 'result_projection_items', None)
                    _producer_metadata = getattr(
                        content, 'result_producer_metadata', None)
                    cache_val, content_len = self._prepare_cache_value(content, fn_name)
                    cache_entry = (
                        cache_val, is_search, 'prefetch', _disp, _eng_bkdn,
                        _vert, _sd)
                    if _projection is not None or _producer_metadata is not None:
                        cache_entry += (_projection,)
                    if _producer_metadata is not None:
                        cache_entry += (_producer_metadata,)
                    _store_tool_result_cache_entry(
                        task, cache_key, cache_entry)
                    injected += 1
                    logger.info('[%s] StreamingToolExec: injected %s into '
                                'dedup cache (%.1fs, %d chars%s)',
                                self._tid, fn_name, elapsed, content_len,
                                ', %d display_results' % len(_disp) if _disp else '')
                except Exception as e:
                    logger.debug('[%s] StreamingToolExec: %s pre-exec failed, '
                                 'deferring to normal pipeline: %s',
                                 self._tid, fn_name, e)
            elif not future.done() and not future.cancelled():
                pending.append((tc_id, future, fn_name, fn_args, t0))

        # Second pass: wait for still-running futures — they're already
        # in-progress and would be executed serially anyway, so waiting
        # is always faster than cancelling + re-executing.
        # BUT: if user aborted, cancel remaining futures immediately.
        _stragglers_timed_out = False
        if pending and task.get('aborted'):
            logger.info('[%s] StreamingToolExec: task aborted — cancelling %d '
                        'pending tool(s): %s',
                        self._tid, len(pending),
                        ', '.join(fn for _, _, fn, _, _ in pending))
            for tc_id, future, fn_name, fn_args, t0 in pending:
                future.cancel()
        elif pending:
            logger.info('[%s] StreamingToolExec: waiting for %d still-running '
                        'tool(s): %s',
                        self._tid, len(pending),
                        ', '.join(fn for _, _, fn, _, _ in pending))
            for tc_id, future, fn_name, fn_args, t0 in pending:
                # Check abort between each future wait
                if task.get('aborted'):
                    logger.info('[%s] StreamingToolExec: abort detected while '
                                'waiting — cancelling remaining', self._tid)
                    future.cancel()
                    continue
                try:
                    # Timeout should match the underlying tool's I/O
                    #   timeout (cross-DC multiplier adjusts for slow
                    #   FUSE/NFS mounts — see lib.cross_dc).  The old
                    #   hard-coded 60s threw away in-flight rg work on
                    #   slow mounts, only for the serial pipeline to
                    #   then re-run the same rg from scratch → wasted
                    #   60s + a fresh full scan.  We now align, so the
                    #   pre-execution's result gets injected.
                    #
                    #   Grace window: _run_grep_subprocess kills the
                    #   subprocess *at* io_timeout and then spends up to
                    #   ~5s collecting partial output.  If we wait for
                    #   exactly io_timeout we race the kill-and-collect
                    #   phase and abandon the in-flight result, only for
                    #   the serial pipeline to re-execute the same query
                    #   for another full io_timeout.  Add a 10s slack so
                    #   the partial-results banner gets cached instead.
                    _wait_timeout = 60
                    if fn_name in ('grep_search', 'read_files',
                                   'find_files', 'list_dir'):
                        try:
                            from lib.project_mod.read_tools import _get_io_timeout
                            _wait_timeout = _get_io_timeout(
                                self._project_path or '.', default=60) + 10
                        except Exception as _e:
                            logger.debug('[%s] StreamingToolExec: cross-DC '
                                         'timeout probe unavailable: %s',
                                         self._tid, _e)
                    content = future.result(timeout=_wait_timeout)
                    if not getattr(content, 'cacheable', True):
                        logger.info(
                            '[%s] StreamingToolExec: waited %s outcome is not '
                            'cacheable; deferring to normal pipeline',
                            self._tid, fn_name)
                        continue
                    elapsed = time.time() - t0
                    is_search = fn_name in ('web_search',)
                    cache_key = _make_cache_key(fn_name, fn_args)
                    _disp = getattr(content, 'display_results', None)
                    _eng_bkdn = getattr(content, 'engine_breakdown', None)
                    _vert = getattr(content, 'vertical', None)
                    _sd = getattr(content, 'search_diag', None)
                    _projection = getattr(
                        content, 'result_projection_items', None)
                    _producer_metadata = getattr(
                        content, 'result_producer_metadata', None)
                    cache_val, content_len = self._prepare_cache_value(content, fn_name)
                    cache_entry = (
                        cache_val, is_search, 'prefetch', _disp, _eng_bkdn,
                        _vert, _sd)
                    if _projection is not None or _producer_metadata is not None:
                        cache_entry += (_projection,)
                    if _producer_metadata is not None:
                        cache_entry += (_producer_metadata,)
                    _store_tool_result_cache_entry(
                        task, cache_key, cache_entry)
                    injected += 1
                    logger.info('[%s] StreamingToolExec: waited and injected '
                                '%s into dedup cache (%.1fs, %d chars%s)',
                                self._tid, fn_name, elapsed, content_len,
                                ', %d display_results' % len(_disp) if _disp else '')
                except (TimeoutError, _FuturesTimeoutError):
                    _stragglers_timed_out = True
                    logger.warning('[%s] StreamingToolExec: %s timed out after '
                                   '%ds, deferring to normal pipeline',
                                   self._tid, fn_name, _wait_timeout)
                except Exception as e:
                    logger.debug('[%s] StreamingToolExec: %s pre-exec failed, '
                                 'deferring to normal pipeline: %s',
                                 self._tid, fn_name, e)

        # Shutdown thread pool — cancel futures on abort, wait otherwise.
        # ``dispatch`` also calls this idempotently from ``finally`` so a
        # provider break/exception cannot strand queued speculative closures.
        # If any future timed out above it is still running; waiting again
        # (wait=True) would re-block the round past its deadline on the same
        # stragglers. Cancel the pending subset and return without waiting,
        # while keeping the normal fast path blocking.
        _aborted = task.get('aborted', False)
        if _stragglers_timed_out:
            self.close(cancel_futures=True, wait=False)
        else:
            self.close(cancel_futures=_aborted, wait=not _aborted)

        _total = self._submitted_count
        if _total > 0:
            logger.info('[%s] StreamingToolExec summary: %d submitted, '
                        '%d pre-computed and injected into cache',
                        self._tid, _total, injected)
        return injected

    @property
    def submitted_count(self) -> int:
        """Number of tools submitted for pre-execution."""
        return self._submitted_count
