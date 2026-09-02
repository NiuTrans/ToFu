# HOT_PATH — _compute_write_breakdown is called per cache-writing round.
# Prefer logger.debug() over logger.info().
"""Prompt-cache ``write`` decomposition for the per-round cost panel.

This is the sole owner of a self-contained pure computation with no I/O,
event emission, or shared run-loop state.

The single public entry point ``_compute_write_breakdown(task, api_rounds,
round_num)`` decomposes a round's ``cache_write_tokens`` into exact sub-items
(``toolResults`` / ``prevOutput`` / ``contextWrite`` / ``recacheBody`` /
``envelope``) that, by construction, sum to EXACTLY ``write``.
"""

from __future__ import annotations

from typing import Any

from lib.cost import normalize_usage
from lib.log import get_logger

logger = get_logger(__name__)


# Realistic ceiling for genuine message JSON/role framing overhead in a
# round's cache `write`. A round's residual (write − toolResults − prevOutput)
# is at most a few hundred tokens of real framing; anything far above this is
# NOT framing — it is the conversation CONTEXT being written to cache. The
# ceiling is applied UNCONDITIONALLY (not just on cache-break rounds): the
# excess is attributed to `recacheBody` (re-billed waste) on a break round and
# to `contextWrite` (legitimate first-time caching) otherwise, so the round-1
# prefix warm-up is never mislabeled as tens of thousands of tokens of
# "message framing".
_ENVELOPE_MAX_TOKENS = 800

# Minimum cache_read drop (vs the previous round) that the write-breakdown
# treats as re-billed body (`recacheBody`) rather than first-time context
# (`contextWrite`). A drop is direct, percentage-independent evidence that
# already-cached body was NOT read back and is being re-written inside `write`.
# It is deliberately independent of detect_cache_break's 5%-relative WARNING
# gate, which (correctly, to avoid banner noise) stays silent on a small-percent
# but real-cost drop — e.g. a 4.9k re-bill on a 135k read is only ~3.6% so
# api_break never fires, and the excess used to be mislabeled "first-time
# context, not waste". Matches the project's _MIN_CACHE_MISS_TOKENS floor.
_READ_DROP_WASTE_TOKENS = 2000


def _compute_write_breakdown(task: dict[str, Any], api_rounds: list,
                             round_num: int,
                             prev_turn_cache_read: int = 0) -> dict[str, int] | None:
    """Decompose a round's prompt-cache ``write`` into exact sub-items.

    A round's ``cache_write_tokens`` is the new context cached since the
    previous cached point. It is NOT what the model generated this round; it
    is composed of three parts, each computed here from REAL recorded numbers
    (never a hand-labeled lump):

    * ``toolResults`` — the tool RESULTS fed back into the prefix = the sum of
      ``toolTokens`` over the tool rounds whose ``llmRound`` is the PREVIOUS
      LLM iteration (``round_num - 1``); these are the per-tool token counts
      the ptool-panel badges show (``_safe_count_tokens`` of each result).
    * ``prevOutput`` — the previous API round's assistant output (model text +
      reasoning + serialized ``tool_call`` argument blocks), read from that
      round's recorded ``usage`` (``completion_tokens``/``output_tokens`` +
      ``reasoning_tokens``/``thinking_tokens``).
    * ``contextWrite`` — conversation CONTEXT (system prompt, tool definitions,
      history messages) written to cache for the FIRST time. Dominant on
      round 1 (the prefix warm-up), and on any round that appends a large fresh
      chunk of context. This is the unavoidable, non-wasteful cost of warming
      the cache — the next round reads it back. Recognized by ``cache_read``
      holding or GROWING vs the previous round (the new context is added on top
      of a still-cached prefix).
    * ``recacheBody`` — context we ALREADY paid to cache being re-billed because
      the server didn't read it back (genuine waste; see ``recacheCause``).
      Recognized EITHER by a confirmed ``cacheBreak`` flag OR — independently of
      that banner-level alarm — by ``cache_read`` DROPPING vs the previous round
      (``readDrop`` ≥ ``_READ_DROP_WASTE_TOKENS``): a drop is direct evidence
      that already-cached body fell out and is now inside ``write``. The
      ``detect_cache_break`` 5%-relative gate stays silent on a small-percent
      but real-cost drop (e.g. 4.9k re-billed on a 135k read ≈ 3.6%), so relying
      on it alone mislabeled that waste as benign ``contextWrite``. When the
      excess exceeds the read drop, only the drop is ``recacheBody`` and the
      remainder is genuine new ``contextWrite``; the two split the excess.
    * ``envelope`` — the message JSON/role framing overhead, the residual
      ``write - prevOutput - toolResults - (contextWrite|recacheBody)`` capped
      at ``_ENVELOPE_MAX_TOKENS``. By construction the sub-items sum to EXACTLY
      ``write``.

    Args:
        task: Live task dict (read-only here) — used for ``toolRounds``.
        api_rounds: The per-LLM-round usage list. ``api_rounds[-1]`` is the
            round just recorded (round ``round_num + 1``); ``api_rounds[-2]``
            is the previous round (round ``round_num``), whose output became
            part of this round's write.
        round_num: Zero-based orchestrator loop index of the CURRENT iteration.
        prev_turn_cache_read: The CROSS-TURN ``cache_read`` baseline — the
            previous USER TURN's final cached-prefix read, recovered across the
            ``run_task`` thread boundary (see
            ``cache_tracking.get_prev_turn_cache_read``). Used ONLY when this
            round has no within-turn predecessor (``api_rounds[-2]`` absent, i.e.
            the turn's round-1): without it round-1 had NO read baseline and
            defaulted the whole ``write`` to benign ``contextWrite`` even when
            the previous turn's cached prefix was partly evicted and re-billed
            this round (the round-1 mislabel). Defaults to 0 (genuine first-ever
            call → still ``contextWrite``, correctly).

    The residual above ``_ENVELOPE_MAX_TOKENS`` is ALWAYS context, not framing
    — a 64k round-1 "envelope" (the symptom that motivated this) is the whole
    system+tools+history prefix being cached for the first time. It is split
    into ``contextWrite`` (first-time caching, no break) or ``recacheBody``
    (re-billed body, on a break round); ``envelope`` keeps only a realistic
    framing allowance. All sub-items still sum to ``write``.

    Returns:
        ``{'write', 'toolResults', 'prevOutput', 'contextWrite', 'recacheBody',
        'envelope', 'recacheCause', 'capped'}`` token dict, or ``None`` when this round
        wrote no cache (nothing to decompose) or the inputs are missing.
        Returning ``None`` keeps the frontend on its plain inflow line rather
        than printing a meaningless breakdown.
    """
    try:
        if not api_rounds:
            return None
        _cur = api_rounds[-1]
        if not isinstance(_cur, dict):
            return None
        _u = _cur.get('usage') or {}
        _nu = normalize_usage(_u)
        write = _nu['cache_write']
        if write <= 0:
            return None

        # (a) tool results that flowed into THIS round's prefix: the tools that
        #     ran in the previous LLM iteration (llmRound == round_num - 1).
        tool_results = 0
        _prev_llm_round = round_num - 1
        for _r in (task.get('toolRounds') or []):
            if not isinstance(_r, dict):
                continue
            if _r.get('llmRound') == _prev_llm_round:
                _tt = _r.get('toolTokens')
                if isinstance(_tt, (int, float)) and _tt > 0:
                    tool_results += int(_tt)

        # (b) previous API round's output tokens (text + reasoning + tool_call
        #     args). The previous round is api_rounds[-2]; fall back to 0 when
        #     this is the first recorded round (no predecessor).
        prev_output = 0
        if len(api_rounds) >= 2 and isinstance(api_rounds[-2], dict):
            _pu = api_rounds[-2].get('usage') or {}
            _npu = normalize_usage(_pu)
            prev_output = _npu['output'] + _npu['thinking']

        # (b2) cache_read delta vs the previous round. A DROP means part of the
        #     previously-cached prefix was not read back this round and is being
        #     re-billed inside `write` — direct, percentage-independent evidence
        #     of waste. This is what distinguishes a large write that is genuine
        #     first-time context (read held/grew) from one that is re-cached
        #     body (read fell), WITHOUT relying on detect_cache_break's
        #     5%-relative warning gate.
        cur_read = _nu['cache_read']
        prev_read = 0
        _have_within_turn_prev = (len(api_rounds) >= 2
                                  and isinstance(api_rounds[-2], dict))
        if _have_within_turn_prev:
            _pru = api_rounds[-2].get('usage') or {}
            prev_read = normalize_usage(_pru)['cache_read']
        else:
            # No within-turn predecessor — this is the turn's round-1. Fall back
            # to the CROSS-TURN baseline (previous turn's final cached-prefix
            # read, carried across the run_task thread boundary). This is the
            # traceability fix: round-1 previously had prev_read=0 → read_drop=0
            # → the whole write defaulted to benign contextWrite, so an evicted
            # tail re-billed this turn was mislabeled "first-cache warm-up".
            # With a real prior-turn baseline, a round-1 read below it is
            # correctly attributed to recacheBody. 0 (genuine first-ever call)
            # leaves read_drop=0 → contextWrite, as before.
            prev_read = int(prev_turn_cache_read or 0)
        read_drop = max(0, prev_read - cur_read)

        # (c) envelope = the genuine residual. INVARIANT: the three sub-items
        #     MUST sum to exactly `write` (the whole point — a breakdown that
        #     doesn't add up is worse than none). prev_output / tool_results
        #     are counted with a DIFFERENT (output-side / local) tokenizer than
        #     the provider's input-side `cache_write_tokens`, so their SUM can
        #     legitimately overshoot `write`. When that happens we must NOT
        #     print components that exceed the total.
        #
        #     The old resolution capped in priority order (tool results first,
        #     then prev output) — but that let a HIGH tool-result estimate eat
        #     the entire `write` and zero out `prevOutput`, wrongly attributing
        #     the previous round's real assistant output to "tool results"
        #     (the round-2 bug: write 1.1k, tool est 1.6k → prevOutput 595 was
        #     zeroed and its share mislabeled tool results). Resolve instead by
        #     scaling BOTH measured components down PROPORTIONALLY to their
        #     estimated share, so neither is annihilated by the other's
        #     overshoot. prev_output takes the exact remainder so the two still
        #     sum to `write` with no rounding drift. When they do NOT overshoot,
        #     nothing is scaled and the residual flows on to envelope/context.
        _measured = tool_results + prev_output
        _capped = _measured > write
        if _capped:
            tool_results = round(write * tool_results / _measured) if _measured else 0
            prev_output = write - tool_results  # exact remainder → sum == write
            residual = 0
        else:
            residual = write - tool_results - prev_output  # always >= 0

        # (d) Split the residual. On a NORMAL round the residual is just the
        #     message JSON/role framing overhead (tens of tokens/message) — a
        #     few hundred tokens. On a CACHE-BREAK round the residual is huge
        #     (tens of thousands) because the conversation BODY between the
        #     static prefix and the tail was re-cached: a prefix-byte mutation
        #     or a breakpoint advance re-billed the whole body uncached. Lumping
        #     that into "envelope" is the lie the user caught (36.5k of
        #     "structure"). So when this round carries a cacheBreak signal,
        #     attribute the bulk to `recacheBody` and leave only a realistic
        #     framing allowance as `envelope`. The four sub-items still sum to
        #     EXACTLY `write` (recacheBody is the residual minus the allowance).
        # Framing is fundamentally BOUNDED, so cap envelope unconditionally and
        # attribute the excess by cause: on a cache-break round it is body we
        # already cached being re-billed (`recacheBody`, waste); otherwise it is
        # context cached for the first time (`contextWrite`, e.g. the round-1
        # prefix warm-up — legitimate, not framing). Exactly one of the two is
        # ever non-zero. The five sub-items still sum to EXACTLY `write`.
        _cache_break = _cur.get('cacheBreak')
        recache_body = 0
        context_write = 0
        envelope = residual
        if residual > _ENVELOPE_MAX_TOKENS:
            envelope = _ENVELOPE_MAX_TOKENS
            excess = residual - envelope
            if _cache_break:
                # Confirmed break (detect_cache_break fired) → the whole excess
                # is re-billed body.
                recache_body = excess
            elif read_drop >= _READ_DROP_WASTE_TOKENS:
                # No banner-level break, but cache_read fell vs the previous
                # round: at least `read_drop` tokens of already-cached body are
                # being re-written (waste). Attribute that to recacheBody and
                # only the remainder — genuinely new context — to contextWrite.
                recache_body = min(excess, read_drop)
                context_write = excess - recache_body
            else:
                # Read held or grew → the excess is fresh context cached for the
                # first time (e.g. the round-1 prefix warm-up). Legitimate.
                context_write = excess

        return {
            'write': write,
            'toolResults': tool_results,
            'prevOutput': prev_output,
            'contextWrite': context_write,
            'recacheBody': recache_body,
            'envelope': envelope,
            # How far cache_read fell vs the previous round (0 if it held/grew).
            # Lets the frontend explain a recacheBody term that the banner-level
            # detector did not flag.
            'readDrop': read_drop,
            # The cache-break cause string (if any) that drove the re-cache —
            # lets the frontend tie the recacheBody term to the 缓存失效 line.
            # When recacheBody is driven by a sub-threshold read drop (no formal
            # break), synthesize a cause so the term is never left unexplained.
            'recacheCause': (
                _cache_break if isinstance(_cache_break, dict)
                else ({'no_cache_reuse':
                       f'cache_read 较上一轮下降 {read_drop} tok（已缓存正文被重新计费）'}
                      if recache_body > 0 else {})
            ),
            # True when the measured components had to be scaled because their
            # SUM exceeded `write` (output-side vs input-side tokenizer
            # mismatch). The frontend notes the figures are approximate then.
            'capped': _capped,
        }
    except Exception as e:
        logger.debug('write-breakdown compute failed: %s', e)
        return None
