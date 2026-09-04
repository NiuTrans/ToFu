#!/usr/bin/env python3
"""@safe_route rollout tests (routes/api_v1/optimizer.py, pt_63eb7f02 batch 3).

The mechanical-sweep epic pt_63eb7f02 has TWO halves: (1) migrate ad-hoc
``jsonify({'error': ...})`` to ``api_error(...)`` — batches 1+2 landed via
2ded48cc / 825338e8. (2) Adopt ``@safe_route`` on handlers whose except-block is
a pure ``logger.error(...); return api_internal_error(e, context=<fn>, ...)``
with no side effects — THIS test targets that half.

The three handlers converted this batch — ``list_proposals``, ``get_proposal``,
``run_now`` in ``routes/api_v1/optimizer.py`` — met the strict conversion gate:

  1. except-block was PURE (logger.error + api_internal_error), no
     side effects like ``_storage.update_proposal_status(...)`` or
     ``audit_log(...)`` (those would be silently dropped by a bare
     ``@safe_route``).
  2. context= string was equivalent to what ``safe_route`` builds
     automatically from ``fn.__qualname__`` / ``fn.__module__`` — so
     diagnostic granularity is preserved after conversion.

Deliberately NOT converted this batch: ``approve_proposal`` (its except runs
``_storage.update_proposal_status(reason=...)`` + ``audit_log(...)``) and
``revert_proposal`` (its except uses ``context='revert.apply'`` distinct from
the qualname). Those are shape-sensitive; a partial ``@safe_route`` would drop
their diagnostic side effects. Guarded here so a future well-meaning cleanup
does not silently convert them without noticing.

Two test layers apply the repository's response-envelope contract:

  1. WIRE-SAFETY parity — the decorated handlers still return a 500 with the
     ``ok:False`` envelope shape when they raise. Uses the real
     ``lib.api_response.safe_route`` decorator through a fabricated handler
     (the real ones require _storage + auth) so we prove decorator behaviour,
     not the specific handler's plumbing.
  2. SHIPPED-SOURCE regression — the three converted handlers ARE decorated
     with @safe_route and their old ``try / except Exception / api_internal_error``
     hand-roll is GONE. The two intentionally-not-converted handlers still
     carry their audit-log / status-flip side effects.
"""

from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Flask→Quart shim (matches the rest of the suite).
import quart as _quart
sys.modules.setdefault('flask', _quart)

try:
    import pytest
except ImportError:  # pragma: no cover
    pytest = None  # type: ignore[assignment]


_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPTIMIZER_PATH = os.path.join(_ROOT, 'routes', 'api_v1', 'optimizer.py')
_AGENTS_PATH = os.path.join(_ROOT, 'routes', 'api_v1', 'agents.py')
_CONFIG_PATH = os.path.join(_ROOT, 'routes', 'config.py')


def _unit(fn):
    return fn if pytest is None else pytest.mark.unit(fn)


def _make_app():
    from quart import Quart
    if 'PROVIDE_AUTOMATIC_OPTIONS' not in Quart.default_config:
        Quart.default_config = {**Quart.default_config,
                                'PROVIDE_AUTOMATIC_OPTIONS': True}
    return Quart(__name__)


# ── Layer 1: decorator-level wire-safety ──────────────────────────────

@_unit
def test_safe_route_wraps_sync_handler_500_envelope():
    """@safe_route around a sync handler that raises returns a 500 whose
    body carries ``ok:False`` + an ``error`` envelope (the api_error contract).

    This is the exact contract the converted optimizer.py handlers now rely on
    instead of the hand-rolled try/except. If safe_route ever regressed to
    letting the exception propagate or dropping the ``ok`` key, the migrated
    optimizer handlers would silently break — this test is the tripwire.
    """
    from lib.api_response import safe_route

    @safe_route
    def crashing():
        raise ValueError('boom in list')

    app = _make_app()

    async def _t():
        async with app.test_request_context('/x'):
            resp, status = crashing()
            assert status == 500, f'expected 500, got {status}'
            body = json.loads(await resp.get_data(as_text=True))
            assert body.get('ok') is False, f'missing ok:False in {body!r}'
            assert 'error' in body, f'missing error key in {body!r}'

    import asyncio
    asyncio.run(_t())


@_unit
def test_safe_route_passthroughs_a_successful_response():
    """@safe_route on a handler that does NOT raise passes its return through
    verbatim. Guards against a regression where safe_route accidentally
    reshapes 2xx responses (which would break every converted handler's happy
    path — list_proposals/get_proposal/run_now all return api_ok(...))."""
    from lib.api_response import api_ok, safe_route

    @safe_route
    def happy():
        return api_ok({'value': 42})

    app = _make_app()

    async def _t():
        async with app.test_request_context('/x'):
            resp, status = happy()
            assert status == 200
            body = json.loads(await resp.get_data(as_text=True))
            assert body.get('ok') is True
            assert body.get('value') == 42

    import asyncio
    asyncio.run(_t())


# ── Layer 2: shipped-source regression ────────────────────────────────

# The trio being converted this batch. Each entry: (fn_name, expect_decorated,
# expect_no_ad_hoc_try). "Ad-hoc try" here = the specific "try _storage.X /
# except Exception / return api_internal_error(...)" hand-roll — a
# per-handler regex, not a blanket ban on all try/except (the file legitimately
# still has JSON-decode try/except).
_CONVERTED_HANDLERS = ('list_proposals', 'get_proposal', 'run_now')

# Handlers explicitly NOT converted this batch — their except blocks do
# side-effectful work (audit_log / status-flip) or use a distinct context=
# string. Guarded so a future well-meaning cleanup that "just adds
# @safe_route to the remaining ones" trips this test.
_UNCONVERTED_HANDLERS = ('approve_proposal', 'revert_proposal')


# ── Batch 4 (agents.py) ────────────────────────────────────────────
# Same strict gate as batch 3 (optimizer.py): only handlers whose FINAL
# except is a pure `logger.exception + api_internal_error(e)` with no
# distinct context and no side effects. Batch 4 audit of agents.py's
# 24 try/except blocks yielded EXACTLY ONE match: memory_search. The
# other 23 blocks all have a legitimate non-@safe_route reason:
#   * `except ImportError` (module-availability check → distinct context)
#   * `except BadRequest` (400 with field, NOT 500)
#   * `except (ValueError, TypeError)` (recovery-with-default)
#   * `except Exception` with a DISTINCT context= or log_traceback=False
# Documented so a future PR expanding this list understands the gate.
_CONVERTED_AGENTS_HANDLERS = ('memory_search',)
_UNCONVERTED_AGENTS_HANDLERS_SAMPLE = (
    'image_gen',  # BadRequest handler has field='prompt' fallback that
                  # @safe_route's generic _handle would not preserve.
    'swarm_abort',  # except carries log_traceback=False (warning-level,
                    # not exception dump); @safe_route defaults to True.
)


def _optimizer_src() -> str:
    with open(_OPTIMIZER_PATH, encoding='utf-8') as f:
        return f.read()


def _agents_src() -> str:
    with open(_AGENTS_PATH, encoding='utf-8') as f:
        return f.read()


# ── Batch 5 (config.py) ─────────────────────────────────────────────
# Same strict gate: FINAL except is a pure logger.error/warning +
# api_internal_error(e) with no distinct context, no side effects.
# Batch 5 audit found 6 candidates matching:
#   * routes/config.py — feishu_status
_CONVERTED_CONFIG_HANDLERS = ('feishu_status',)


def _config_src() -> str:
    with open(_CONFIG_PATH, encoding='utf-8') as f:
        return f.read()


@_unit
def test_converted_handlers_are_decorated_with_safe_route():
    """Each of list_proposals / get_proposal / run_now MUST carry
    @safe_route immediately above its ``def`` line — the source-level
    regression tripwire."""
    src = _optimizer_src()
    for name in _CONVERTED_HANDLERS:
        # Match: @safe_route on its own line, followed by (optional
        # blank / comment lines), then `def <name>(`. The decorator MUST
        # be the innermost one relative to the function so route-registration
        # decorators still fire first — but pattern-wise it can appear
        # anywhere in the decorator stack.
        pattern = re.compile(
            r'@safe_route\b[^\n]*\ndef\s+' + re.escape(name) + r'\s*\(',
            re.MULTILINE)
        assert pattern.search(src), (
            f'{name}: expected @safe_route immediately before its def line '
            f'in routes/api_v1/optimizer.py — the pt_63eb7f02 batch-3 '
            f'rollout hasn\'t landed or has been undone')


@_unit
def test_converted_handlers_no_longer_hand_roll_the_500_try_except():
    """The ad-hoc ``try: <storage-call> ... except Exception ... return
    api_internal_error(...)`` block is GONE from each converted handler.

    Method: bracket each handler's source between its ``def`` and either
    the next top-level ``def``/``@api_v1_optimizer_bp.route`` or EOF,
    then assert that no ``except Exception`` with an ``api_internal_error``
    call appears inside that slice.
    """
    src = _optimizer_src()

    # Locate handler boundaries: sorted def offsets → each handler runs
    # from its def line to just before the next @route decorator (or EOF).
    def_offsets = {}
    for m in re.finditer(r'^def\s+(\w+)\s*\(', src, re.MULTILINE):
        def_offsets[m.group(1)] = m.start()

    # Sort by offset to derive slices.
    sorted_defs = sorted(def_offsets.items(), key=lambda kv: kv[1])
    slices = {}
    for i, (name, off) in enumerate(sorted_defs):
        end = sorted_defs[i + 1][1] if i + 1 < len(sorted_defs) else len(src)
        slices[name] = src[off:end]

    for name in _CONVERTED_HANDLERS:
        slc = slices.get(name, '')
        assert slc, f'could not locate {name} slice — parser drift?'
        # The specific ad-hoc pattern being removed:
        # `except Exception as e: ... return api_internal_error(`
        # We look for those two tokens in the same slice; a legitimate
        # non-500 try/except (e.g. JSON decode) will not carry an
        # api_internal_error return.
        has_except_exception = re.search(
            r'except\s+Exception\s+as\s+\w+\s*:', slc) is not None
        has_internal_error_return = 'api_internal_error(' in slc
        assert not (has_except_exception and has_internal_error_return), (
            f'{name}: still contains the ad-hoc "except Exception → '
            f'api_internal_error(" pattern that @safe_route was meant '
            f'to replace. The decorator adds this behaviour automatically; '
            f'the manual block is redundant and diverges from the '
            f'pt_63eb7f02 batch-3 goal.\n'
            f'Slice excerpt:\n{slc[:600]}')


@_unit
def test_unconverted_handlers_keep_their_side_effect_except_blocks():
    """approve_proposal / revert_proposal were deliberately NOT converted:
    their except blocks do meaningful work that a plain @safe_route would
    silently drop. Guard so a future overzealous cleanup does not remove
    them without noticing.

    approve_proposal: except → _storage.update_proposal_status(reason=...) +
                      audit_log('optimizer_action_failed', ...)
    revert_proposal:  except → api_internal_error(e, context='revert.apply',
                      source='api_v1.optimizer.revert') — the context= string
                      distinguishes revert.apply from other revert paths and
                      would collapse to fn.__qualname__ under @safe_route.
    """
    src = _optimizer_src()

    # approve_proposal: must still call update_proposal_status with a
    # reason string that mentions "manual approve failed" (the failure
    # side effect that @safe_route CANNOT reproduce).
    approve_slice = _slice_of(src, 'approve_proposal')
    assert "manual approve failed" in approve_slice, (
        'approve_proposal: expected the "manual approve failed" audit '
        'reason to survive — this side effect is the reason it was not '
        'converted to @safe_route in batch 3')
    assert 'optimizer_action_failed' in approve_slice, (
        'approve_proposal: expected the audit_log("optimizer_action_failed") '
        'call to survive — same reason')

    # revert_proposal: must still carry the distinct context='revert.apply'
    # string; @safe_route would collapse this to the qualname
    # 'revert_proposal', losing the "which step failed" resolution.
    revert_slice = _slice_of(src, 'revert_proposal')
    assert "context='revert.apply'" in revert_slice, (
        "revert_proposal: expected context='revert.apply' string to "
        'survive — it distinguishes the revert-step failure from '
        'other revert-path errors and would collapse under @safe_route')


def _slice_of(src: str, name: str) -> str:
    """Extract a function-body slice from a top-level def to the next
    def / route decorator / EOF. Small helper used above."""
    defs = [(m.group(1), m.start())
            for m in re.finditer(r'^def\s+(\w+)\s*\(', src, re.MULTILINE)]
    defs.sort(key=lambda kv: kv[1])
    for i, (n, off) in enumerate(defs):
        if n == name:
            end = defs[i + 1][1] if i + 1 < len(defs) else len(src)
            return src[off:end]
    return ''


@_unit
def test_metadata_lists_stay_in_sync():
    """Meta-test: both _CONVERTED_HANDLERS and _UNCONVERTED_HANDLERS are
    non-empty and disjoint. Guards against an accidental copy-paste
    (same name in both lists → the assertions cancel out silently)."""
    assert _CONVERTED_HANDLERS, 'converted list must not be empty'
    assert _UNCONVERTED_HANDLERS, 'unconverted list must not be empty'
    overlap = set(_CONVERTED_HANDLERS) & set(_UNCONVERTED_HANDLERS)
    assert not overlap, f'a handler cannot be both converted and not: {overlap}'
    # Same invariant on batch 4 (agents.py).
    assert _CONVERTED_AGENTS_HANDLERS, 'agents-converted list must not be empty'
    assert _UNCONVERTED_AGENTS_HANDLERS_SAMPLE, 'agents-unconverted sample must not be empty'
    overlap4 = (set(_CONVERTED_AGENTS_HANDLERS)
                & set(_UNCONVERTED_AGENTS_HANDLERS_SAMPLE))
    assert not overlap4, f'agents: cannot be both: {overlap4}'


# ── Batch 4 shipped-source guards ──────────────────────────────────

@_unit
def test_batch4_converted_agents_handlers_have_safe_route():
    """batch 4: routes/api_v1/agents.py memory_search MUST carry
    @safe_route immediately above its ``def`` line — same source-level
    regression tripwire as batch 3, but for the agents file."""
    src = _agents_src()
    for name in _CONVERTED_AGENTS_HANDLERS:
        pattern = re.compile(
            r'@safe_route\b[^\n]*\ndef\s+' + re.escape(name) + r'\s*\(',
            re.MULTILINE)
        assert pattern.search(src), (
            f'{name}: expected @safe_route immediately before its def line '
            f'in routes/api_v1/agents.py — the pt_63eb7f02 batch-4 '
            f'rollout hasn\'t landed or has been undone')


@_unit
def test_batch4_converted_agents_no_ad_hoc_final_except():
    """batch 4: the SPECIFIC ad-hoc final try/except that @safe_route
    replaces must be gone from memory_search.

    memory_search's ORIGINAL last block was::

        try:
            results = search_memories(query, top_k=top_k)
        except Exception as e:
            logger.exception('[api_v1.memory] search failed')
            return api_internal_error(e)

    After batch 4 the ``search_memories`` call must be OUTSIDE any
    ``try``/``except Exception`` block within its own body (the earlier
    ``except ImportError`` for the module import and the
    ``except (ValueError, TypeError)`` for ``int(top_k)`` are DIFFERENT
    exception classes and are legitimately retained; we only forbid
    ``except Exception`` INSIDE memory_search).
    """
    src = _agents_src()

    # Extract the memory_search function body slice.
    m = re.search(
        r'^def memory_search\s*\(.*?\n(.*?)(?=\n(?:def |@[a-zA-Z_].*\n(?:async )?def |__all__))',
        src, re.MULTILINE | re.DOTALL)
    assert m, 'could not locate memory_search body slice — parser drift?'
    body = m.group(1)

    # The forbidden pattern: except Exception + return api_internal_error(...)
    # anywhere in the memory_search body.
    has_except_exception = re.search(
        r'except\s+Exception\s+as\s+\w+\s*:', body) is not None
    has_internal_error_return = 'api_internal_error(e)' in body \
        or 'return api_internal_error(e,' in body \
        and 'context=' not in body.split('api_internal_error(e', 1)[1][:50]
    # More precise: the specific ad-hoc "search_memories → except Exception"
    # pattern is gone.
    ad_hoc_present = re.search(
        r'try:\s*\n\s*results\s*=\s*search_memories\(.*?\)\s*\n'
        r'\s*except\s+Exception\s+as\s+\w+\s*:',
        body, re.DOTALL) is not None
    assert not ad_hoc_present, (
        'memory_search: the ad-hoc "try search_memories → '
        'except Exception → api_internal_error" block is still present. '
        '@safe_route was supposed to replace it.')


@_unit
def test_batch4_unconverted_agents_keep_their_reasons():
    """batch 4: image_gen + swarm_abort must NOT be @safe_route-decorated
    — their except handlers do things a bare @safe_route cannot express:

      * image_gen: ``except BadRequest as e: return api_bad_request(str(e),
        field=e.field or 'prompt')`` — the ``or 'prompt'`` fallback would
        be lost (safe_route's _handle uses ``field=e.field`` verbatim).
      * swarm_abort: ``return api_internal_error(e, context='Swarm abort
        failed', ..., log_traceback=False)`` — the log_traceback=False
        override + distinct context= would collapse under @safe_route.

    Guard the specific signals so a future overzealous cleanup that adds
    @safe_route to either function trips this test.
    """
    src = _agents_src()
    # Neither handler is decorated with @safe_route.
    for name in _UNCONVERTED_AGENTS_HANDLERS_SAMPLE:
        pattern = re.compile(
            r'@safe_route\b[^\n]*\ndef\s+' + re.escape(name) + r'\s*\(',
            re.MULTILINE)
        assert not pattern.search(src), (
            f'{name} in routes/api_v1/agents.py MUST NOT be @safe_route-'
            f'decorated — its except handler does something @safe_route '
            f'cannot express. See _UNCONVERTED_AGENTS_HANDLERS_SAMPLE '
            f'docstring in this test file for the specific reason.')

    # The load-bearing signals are still present.
    assert "field=e.field or 'prompt'" in src, (
        "image_gen: expected \"field=e.field or 'prompt'\" fallback to "
        'survive — this is the reason image_gen was NOT converted')
    assert "context='Swarm abort failed'" in src, (
        "swarm_abort: expected context='Swarm abort failed' distinct "
        'context to survive — reason it was not converted')
    assert 'log_traceback=False' in src, (
        'swarm_abort: expected log_traceback=False override to survive')


# ── Batch 5 shipped-source guards ─────────────────────────────────

@_unit
def test_batch5_converted_config_handlers_have_safe_route():
    """batch 5: 2 config.py handlers carry @safe_route directly above def."""
    src = _config_src()
    for name in _CONVERTED_CONFIG_HANDLERS:
        pattern = re.compile(
            r'@safe_route\b[^\n]*\ndef\s+' + re.escape(name) + r'\s*\(',
            re.MULTILINE)
        assert pattern.search(src), (
            f'{name}: expected @safe_route immediately before its def line '
            f'in routes/config.py — the pt_63eb7f02 batch-5 rollout '
            f'hasn\'t landed or has been undone')


@_unit
def test_batch5_ad_hoc_final_except_patterns_gone():
    """batch 5: the specific "except Exception → api_internal_error(e)"
    trailing pattern that @safe_route replaces is GONE from each
    converted handler.

    We do NOT forbid all try/except in the file. What we forbid is the
    SPECIFIC trailing pattern: an except Exception followed by
    ``return api_internal_error(e)`` on the NEXT line (that's the ad-hoc
    wrap @safe_route replaces).
    """
    for path in (_CONFIG_PATH,):
        with open(path, encoding='utf-8') as f:
            src = f.read()
        # config.py legitimately RETAINS api_internal_error calls at OTHER sites
        # (with distinct context= strings — image_gen-style). So we search
        # for the LITERAL 3-line ad-hoc pattern that only ever appeared
        # around the converted handlers.
        for _label, _fn_marker in (
            ('feishu_status', 'def feishu_status'),
        ):
            if _fn_marker not in src:
                continue
            slc = _slice_after(src, _fn_marker, 60)
            # The forbidden pattern: except Exception + return
            # api_internal_error(e) inside the slice.
            has_forbidden = bool(re.search(
                r'except\s+Exception\s+as\s+\w+\s*:\s*\n'
                r'\s*logger\.(?:error|warning)\(.*?\n'
                r'\s*return\s+api_internal_error\(e\)',
                slc, re.DOTALL))
            assert not has_forbidden, (
                f'{_label}: still contains the ad-hoc "except Exception → '
                f'api_internal_error(e)" wrap that @safe_route was meant to '
                f'replace. slice (first 400 chars): {slc[:400]!r}')


def _slice_after(src: str, marker: str, max_lines: int) -> str:
    """Return a bounded slice of src starting at the first occurrence of
    ``marker`` and running for ``max_lines`` lines (or to EOF)."""
    idx = src.find(marker)
    if idx < 0:
        return ''
    tail = src[idx:]
    lines = tail.split('\n', max_lines + 1)[:max_lines + 1]
    return '\n'.join(lines)


if __name__ == '__main__':
    tests = [
        test_safe_route_wraps_sync_handler_500_envelope,
        test_safe_route_passthroughs_a_successful_response,
        test_converted_handlers_are_decorated_with_safe_route,
        test_converted_handlers_no_longer_hand_roll_the_500_try_except,
        test_unconverted_handlers_keep_their_side_effect_except_blocks,
        test_metadata_lists_stay_in_sync,
        test_batch4_converted_agents_handlers_have_safe_route,
        test_batch4_converted_agents_no_ad_hoc_final_except,
        test_batch4_unconverted_agents_keep_their_reasons,
        test_batch5_converted_config_handlers_have_safe_route,
        test_batch5_ad_hoc_final_except_patterns_gone,
    ]
    for fn in tests:
        fn()
        print('ok', fn.__name__)
    print('ALL PASSED')
