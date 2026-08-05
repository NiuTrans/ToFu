#!/usr/bin/env python3
"""Generate the tool inventory — one machine-readable row per tool × facet.

WHY THIS EXISTS
---------------
Tofu ships ~90 tools. Their SCHEMA and their HANDLER are centralized in
``lib/tools/registry`` + the dispatch ``ToolRegistry``, but a tool's *other*
facets (UI label, approval enricher, gates, serial dispatch, …) live in
separate tables across ``lib/tasks_pkg/tool_dispatch/`` and
``lib/tasks_pkg/handlers/``. Nothing anywhere describes ONE tool completely,
so reviewing 90 tools meant reading ~15 files per tool — in practice nobody
did, and hand-sampling proved unreliable (a manual probe of 8 tools concluded
"one tool lacks a UI label"; the real number is 75).

This script DERIVES that description instead of anyone maintaining it, and
the output doubles as a drift guard (``--check``).

DESIGN RULES (mirrors scripts/gen_frontend_globals.py — see the charter's
"generative declarations" discipline):

* **Derived, never hand-written.** A hand-maintained inventory becomes the
  19th disagreeing copy of the tool list. Editing the generated file is
  pointless: ``--check`` overwrites the argument.
* **Built-ins only in the checked table.** Third-party ``tofu.tools`` entry
  points differ per deployment (this host loads ``liantong_resume``), so
  pinning them in a CI-enforced file makes the gate red on any machine with a
  different plugin set. Plugins are reported in a separate DIAGNOSTIC section
  that ``--check`` ignores — visible, but never a false CI failure.
* **Facts, not judgements.** Each column states what the code does; the
  ratchets in ``tests/test_tool_inventory_generated.py`` decide what is
  acceptable.

NOT HERE, AND WHY — the ``returns`` column (MARKED / CAPPED / UNBOUNDED)
-----------------------------------------------------------------------
An earlier revision tried to derive, per tool, whether its RESULT is bounded
and whether truncation is announced to the model. The intent is sound — a
silently shortened result is strictly worse than a hard error, because the
model treats the surviving half as the whole answer. The column was cut after
five successive designs were each falsified against source-verified tools:

1. handler body only            -> 85/89 UNBOUNDED (handlers merely route)
2. whole modules from sys.modules -> 89/89 MARKED (one match tars the module)
3. AST call graph, depth 2       -> missed ``run_command`` (marker is 3 hops out)
4. + literal dispatch-table resolution + package locality + depth 4
                                 -> 9/9 ground-truth cases correct, but spot
                                    checks found 6 FALSE POSITIVES: the marker
                                    belonged to an unrelated helper on an error
                                    path (``_find_closest_match``,
                                    ``build_result_meta``, ``_chunk_text``).
5. restrict evidence to return-reachable dataflow (transitive closure)
                                 -> killed 3 false positives, introduced 4 new
                                    FALSE NEGATIVES; net worse.

The blocker is structural, not a tuning problem. The dominant idiom among
exactly the tools that truncate is an accumulator mutated by METHOD CALL —
``parts.append(result); return '\n\n'.join(parts)`` (``tool_find_files_batch``).
Tracking that requires modelling container mutation and aliasing, i.e. real
points-to analysis, which is a static-analysis project rather than a column in
a generated document.

So the facet is deliberately ABSENT rather than approximated: a plausible-
looking column that is wrong for a sixth of its rows is worse than no column,
because reviewers would trust it. If it is ever revived, the acceptance bar is
the one used above — a source-verified ground-truth set INCLUDING negative
cases, not just a distribution that looks discriminating.

ALSO NOT HERE — the ``params_ok`` column
----------------------------------------
A second cut column, for a simpler reason: **it had no positive control.** The
rule flagged a param whose prose promises a closed set of values while the
schema declares no ``enum=``, motivated by a precedent where an undocumented
``kind`` got 6/6 generated ideas rejected. Two measurements sank it:

* Fed that exact precedent as a synthetic schema, the rule returned NO defect —
  the prose reads "each idea has a kind: methodology or analysis", which none of
  the trigger phrases (``one of`` / ``either`` / ``valid values`` / …) match. A
  detector that misses its own motivating case is not a detector.
* ``ideate`` is not in this corpus at all: it is an internal pipeline function
  in ``lib/paper/ideate.py``, never exposed as an LLM-facing tool. A broader
  literal-alternation rule then scanned all 160 string params across 90 schemas
  and found exactly one hit, itself a false positive (``run_command``'s
  ``working_dir`` mentions ``python``/``pip`` as example commands, not as
  allowed values).

So the reported ``0`` was a false green — zero because the rule fires on
nothing, not because the tool surface is clean. Any revival must ship a POSITIVE
CONTROL (a known-bad schema the rule provably flags) before its zero means
anything. This is why ``describes_ok`` / ``_confusable_pairs`` is guarded by
both a positive and a negative control in
``tests/test_tool_inventory_generated.py``.

USAGE
    python3 scripts/gen_tool_inventory.py            # write the inventory
    python3 scripts/gen_tool_inventory.py --check    # CI: fail if stale
    python3 scripts/gen_tool_inventory.py --json     # machine consumption
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

OUTPUT_PATH = os.path.join(REPO_ROOT, 'docs', 'TOOL_INVENTORY.md')

#: Facet columns, in report order.
COLUMNS = [
    'tool', 'category', 'spec', 'dispatch', 'write', 'idempotent',
    'label', 'approval_enricher', 'serial', 'read_gate', 'fresh_gate',
    'streamable', 'arg_repair', 'describes_ok',
]


def _load_registry():
    """Import the app far enough that every built-in handler is registered."""
    import lib.tasks_pkg.handlers  # noqa: F401 — registration side-effect
    from lib.tasks_pkg.executor import tool_registry
    from lib.tools import all_specs
    return tool_registry, all_specs()


def _facet_tables():
    """Collect the scattered per-facet tables, each behind its own try.

    A facet whose module fails to import degrades to "unknown" for every tool
    rather than aborting the whole inventory — the point is to SEE the tool
    surface, and a partial view still beats none. Failures are reported on
    stderr so a silently-empty column can't masquerade as "no tool has this".
    """
    tables: dict[str, object] = {}

    def _try(name, fn):
        try:
            tables[name] = fn()
        except Exception as e:  # noqa: BLE001 — diagnostic breadth is the point
            print(f'[gen_tool_inventory] WARNING: facet {name!r} unavailable: {e}',
                  file=sys.stderr)
            tables[name] = None

    def _labels():
        from lib.tasks_pkg.tool_dispatch._labels import tool_label
        return tool_label

    def _enrichers():
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS
        return set(_APPROVAL_META_ENRICHERS)

    def _serial():
        from lib.tasks_pkg.tool_dispatch._heartbeat import _SERIAL_BLOCKING_TOOLS
        return set(_SERIAL_BLOCKING_TOOLS)

    def _partitions():
        from lib.tasks_pkg.tool_dispatch._flags import (
            _IDEMPOTENT_TOOLS, _WRITE_TOOLS,
        )
        return {'write': set(_WRITE_TOOLS), 'idempotent': set(_IDEMPOTENT_TOOLS)}

    def _read_gate():
        from lib.tasks_pkg.handlers._read_gate import _GATED_TOOLS
        return set(_GATED_TOOLS)

    def _fresh_gate():
        from lib.tasks_pkg.handlers._write_freshness_gate import (
            _GATED_BATCH_TOOLS, _GATED_SINGLE_TOOLS,
        )
        return set(_GATED_SINGLE_TOOLS) | set(_GATED_BATCH_TOOLS)

    def _streamable():
        from lib.tasks_pkg.streaming_tool_executor import _STREAMABLE_TOOLS
        return set(_STREAMABLE_TOOLS)

    def _arg_repair():
        from lib.tool_input_repair._transform import _PARAM_ALIASES
        return set(_PARAM_ALIASES)

    _try('label', _labels)
    _try('enricher', _enrichers)
    _try('serial', _serial)
    _try('partitions', _partitions)
    _try('read_gate', _read_gate)
    _try('fresh_gate', _fresh_gate)
    _try('streamable', _streamable)
    _try('arg_repair', _arg_repair)
    return tables


def _tool_schemas() -> dict:
    """tool name → its live OpenAI function schema.

    Assembled through the real registry with every capability switched on, so
    the text examined here is the text the MODEL receives. Families gated on a
    live extension/agent (browser, desktop) are absent from an assembled list
    in CI, so their module constants are read directly as a fallback.
    """
    out: dict = {}
    try:
        from lib.tools import ToolContext, assemble_tool_list
        ctx = ToolContext(
            cfg={}, task_id='inv', project_path='/tmp/inv', project_enabled=True,
            search_mode='multi', search_enabled=True, fetch_enabled=True,
            code_exec_enabled=False, browser_enabled=True, desktop_enabled=True,
            swarm_enabled=True, image_gen_enabled=True,
            human_guidance_enabled=True, scheduler_enabled=True, messages=[])
        tool_list, _ = assemble_tool_list(ctx)
        for t in tool_list or []:
            fn = (t or {}).get('function') or {}
            if fn.get('name'):
                out[fn['name']] = fn
    except Exception as e:  # noqa: BLE001
        print(f'[gen_tool_inventory] WARNING: schema assembly failed: {e}',
              file=sys.stderr)

    import importlib
    for mod, attr in (
        ('lib.tools', 'BROWSER_TOOLS'),
        ('lib.browser.advanced', 'ADVANCED_BROWSER_TOOLS'),
        ('lib.desktop_tools', 'DESKTOP_TOOLS'),
        ('lib.tools.motion_video', 'MOTION_VIDEO_TOOLS'),
        ('lib.memory', 'ALL_MEMORY_TOOLS'),
        ('lib.scheduler.tool_defs', 'SCHEDULER_TOOLS'),
        ('lib.swarm.tools', 'ARTIFACT_TOOLS'),
    ):
        try:
            group = getattr(importlib.import_module(mod), attr, None) or []
        except Exception:  # noqa: BLE001 — an absent optional family is fine
            continue
        for t in (group if isinstance(group, (list, tuple)) else [group]):
            fn = (t or {}).get('function') or {}
            if fn.get('name') and fn['name'] not in out:
                out[fn['name']] = fn
    return out


def _describe_defects(fn: dict) -> list:
    """Presence-only checks. Kept minimal ON PURPOSE — the real description
    defect is cross-tool CONFUSABILITY, which one schema alone cannot see and
    which is therefore computed in :func:`_confusable_pairs`.
    """
    reasons = []
    desc = str(fn.get('description') or '').strip()
    if not desc:
        reasons.append('no description at all')
    return reasons


_STOPWORDS = frozenset('''a an the of to in for on with and or is are be this that
it its from by as at into use uses used using when what which you your not do
does if then than so such can may will should must one all any each per via
tool call calls called return returns returning given only also same other'''.split())


def _first_sentence_tokens(desc: str) -> set:
    """Content-word set of the description's FIRST sentence.

    The first sentence is what a model skims when choosing between tools, and
    stripping stopwords keeps the comparison about subject matter rather than
    shared English scaffolding.
    """
    head = re.split(r'(?<=[.!?])\s|\n', desc.strip(), maxsplit=1)[0]
    words = re.findall(r'[a-z_]{3,}', head.lower())
    return {w for w in words if w not in _STOPWORDS}


#: Jaccard overlap at or above which two same-category tools are reported as
#: confusable. Calibrated on real data (see the discrimination test): the
#: genuine near-duplicate families in this repo land at >=0.5, while unrelated
#: siblings in the same category sit well below it.
_CONFUSABLE_AT = 0.5


def _confusable_pairs(schemas: dict, rows: list) -> list:
    """Same-category tool pairs whose first sentences are near-duplicates.

    THIS is the description defect that costs a turn: the model picks the wrong
    tool. A length or presence check cannot see it — it needs a comparison
    ACROSS tools, which is why this is computed over the whole inventory rather
    than inside the per-tool defect function.

    Restricted to pairs in the same ``category`` because that is the choice the
    model actually faces; two identically-worded tools in unrelated categories
    are disambiguated by the surrounding task.
    """
    by_cat: dict = {}
    for r in rows:
        fn = schemas.get(r['tool'])
        if not fn:
            continue
        toks = _first_sentence_tokens(str(fn.get('description') or ''))
        if len(toks) < 3:
            continue  # too short to compare meaningfully
        by_cat.setdefault(r['category'] or '', []).append((r['tool'], toks))

    pairs = []
    for cat, items in sorted(by_cat.items()):
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, ta = items[i]
                bname, tb = items[j]
                union = ta | tb
                if not union:
                    continue
                score = len(ta & tb) / len(union)
                if score >= _CONFUSABLE_AT:
                    pairs.append((cat, a, bname, round(score, 2),
                                  sorted(ta & tb)[:6]))
    pairs.sort(key=lambda p: -p[3])
    return pairs


def _dispatch_path(registry, name: str) -> str:
    """How ``lookup(name)`` resolves — EXACT / SET / NONE.

    Worth a column: most tools (83 of 90) resolve through a ``_sets`` entry,
    and that asymmetry is exactly what made the plugin-shadow hijack possible.
    ``NONE`` means a schema is advertised to the model with no handler behind
    it — a tool that can be called but never executed.
    """
    if name in registry._exact:
        return 'EXACT'
    if any(name in s for s, _ in registry._sets):
        return 'SET'
    return 'NONE'


def collect() -> dict:
    """Build the full inventory: built-in rows + a plugin diagnostic list."""
    registry, specs = _load_registry()
    t = _facet_tables()
    schemas = _tool_schemas()
    label_fn = t.get('label')
    parts = t.get('partitions') or {}
    write_set = parts.get('write') or set()
    idem_set = parts.get('idempotent') or set()

    builtin_rows, plugin_rows = [], []
    for spec in specs:
        for name in sorted(spec.provides):
            has_label = (label_fn is not None and label_fn(name) != name)
            row = {
                'tool': name,
                'category': spec.category or '',
                'spec': spec.key,
                'dispatch': _dispatch_path(registry, name),
                'write': name in write_set,
                'idempotent': name in idem_set,
                'label': has_label,
                'approval_enricher': name in (t.get('enricher') or set()),
                'serial': name in (t.get('serial') or set()),
                'read_gate': name in (t.get('read_gate') or set()),
                'fresh_gate': name in (t.get('fresh_gate') or set()),
                'streamable': name in (t.get('streamable') or set()),
                'arg_repair': name in (t.get('arg_repair') or set()),
            }
            _fn = schemas.get(name)
            if _fn is None:
                # No live schema resolved (gated family, or a handler-only
                # name like the artifact tools injected per sub-agent).
                row['describes_ok'] = '?'
                row['_describe_reasons'] = []
            else:
                dr = _describe_defects(_fn)
                row['describes_ok'] = not dr
                row['_describe_reasons'] = dr
            if spec.source == 'plugin':
                row['plugin'] = spec.plugin_name
                plugin_rows.append(row)
            else:
                builtin_rows.append(row)

    builtin_rows.sort(key=lambda r: (r['category'], r['tool']))
    plugin_rows.sort(key=lambda r: (r.get('plugin', ''), r['tool']))
    confusable = _confusable_pairs(schemas, builtin_rows)
    _confused = {p[1] for p in confusable} | {p[2] for p in confusable}
    for r in builtin_rows:
        if r['tool'] in _confused:
            r['describes_ok'] = False
            r.setdefault('_describe_reasons', []).append(
                'first sentence near-duplicates a same-category sibling')
    return {'builtin': builtin_rows, 'plugin': plugin_rows,
            'confusable': confusable}


def _mark(v) -> str:
    return '✓' if v is True else ('' if v is False else str(v))


def render(inv: dict) -> str:
    """Render the inventory as Markdown (the committed, --check'd artifact)."""
    b = inv['builtin']
    out: list[str] = []
    out.append('# Tool inventory (GENERATED — do not edit)')
    out.append('')
    out.append('Regenerate with `python3 scripts/gen_tool_inventory.py`; CI pins it')
    out.append('via `tests/test_tool_inventory_generated.py --check`.')
    out.append('')
    out.append('One row per BUILT-IN tool. Every column is derived from the live')
    out.append('registry + the per-facet tables — nothing here is hand-maintained.')
    out.append('Third-party plugin tools vary per deployment and are listed in the')
    out.append('diagnostic section at the end, which `--check` ignores.')
    out.append('')
    out.append(f'Built-in tools: **{len(b)}**')
    out.append('')

    gap_write_no_enricher = [r['tool'] for r in b
                             if r['write'] and not r['approval_enricher']]
    gap_no_label = [r['tool'] for r in b if not r['label']]
    gap_no_handler = [r['tool'] for r in b if r['dispatch'] == 'NONE']

    out.append('## Gaps')
    out.append('')
    out.append('| gap | count | meaning |')
    out.append('|---|---|---|')
    out.append(f'| write tool with no approval enricher | {len(gap_write_no_enricher)} | '
               'the approval dialog renders a bare tool name — the user approves '
               'blind, which the approval module itself calls "worse than not '
               'prompting at all" |')
    out.append(f'| no UI label | {len(gap_no_label)} | the raw tool name is shown '
               'in the activity line |')
    out.append(f'| no reachable handler | {len(gap_no_handler)} | schema advertised '
               'to the model but nothing executes it |')
    gap_desc = [r for r in b if r.get('describes_ok') is False]
    confusable = inv.get('confusable') or []
    out.append(f'| description cannot disambiguate | {len(gap_desc)} | the model '
               'cannot tell this tool apart from its neighbours and picks the '
               'wrong one |')
    out.append(f'| confusable tool pairs | {len(confusable)} | two same-category '
               'tools open with near-identical sentences, so the model picks '
               'the wrong one |')
    out.append('')
    for title, rows, key in (
        ('Tools whose description cannot disambiguate', gap_desc, '_describe_reasons'),
    ):
        if rows:
            out.append(f'{title}:')
            out.append('')
            for r in rows:
                for reason in r.get(key) or []:
                    out.append(f'- `{r["tool"]}` — {reason}')
            out.append('')
    if confusable:
        out.append('Confusable same-category tool pairs '
                   f'(first-sentence overlap >= {_CONFUSABLE_AT}):')
        out.append('')
        for cat, a, bb, score, shared in confusable:
            out.append(f'- [{cat}] `{a}` vs `{bb}` — overlap {score}, '
                       f'shared: {", ".join(shared)}')
        out.append('')
    out.append('')
    if gap_write_no_enricher:
        out.append('Write tools lacking an approval enricher:')
        out.append('')
        for name in gap_write_no_enricher:
            out.append(f'- `{name}`')
        out.append('')

    out.append('## Built-in tools')
    out.append('')
    out.append('| ' + ' | '.join(COLUMNS) + ' |')
    out.append('|' + '---|' * len(COLUMNS))
    for r in b:
        cells = [r['tool'], r['category'], r['spec'], r['dispatch']] + \
                [_mark(r[c]) for c in COLUMNS[4:]]
        out.append('| ' + ' | '.join(cells) + ' |')
    out.append('')

    out.append('## Plugin tools (diagnostic — NOT pinned by --check)')
    out.append('')
    if inv['plugin']:
        out.append('| tool | plugin | dispatch | write |')
        out.append('|---|---|---|---|')
        for r in inv['plugin']:
            out.append(f"| {r['tool']} | {r.get('plugin', '')} | {r['dispatch']} "
                       f"| {_mark(r['write'])} |")
    else:
        out.append('_No third-party plugin tools loaded in this environment._')
    out.append('')
    return '\n'.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--check', action='store_true',
                    help='exit 1 if the committed inventory is stale')
    ap.add_argument('--json', action='store_true',
                    help='dump the raw inventory as JSON to stdout')
    args = ap.parse_args()

    inv = collect()
    if args.json:
        print(json.dumps(inv, indent=2, ensure_ascii=False))
        return 0

    rendered = render(inv)
    if args.check:
        try:
            with open(OUTPUT_PATH, encoding='utf-8') as f:
                current = f.read()
        except OSError:
            print(f'MISSING: {OUTPUT_PATH} — run scripts/gen_tool_inventory.py',
                  file=sys.stderr)
            return 1
        # Compare only the CHECKED portion. The trailing plugin section is
        # documented as diagnostic-only ("--check ignores it"), but the naive
        # whole-file compare DID include it — so a host with a third-party
        # plugin (liantong_resume on the dev box) and a host without (public
        # CI) rendered different files and the gate red-filed CI on
        # 2026-08-05 despite a perfectly in-sync built-in table.
        _DIAG = '\n## Plugin tools'
        current_checked = current.split(_DIAG)[0]
        rendered_checked = rendered.split(_DIAG)[0]
        if current_checked != rendered_checked:
            print(f'STALE: {OUTPUT_PATH} does not match the live registry. '
                  f'Run: python3 scripts/gen_tool_inventory.py', file=sys.stderr)
            return 1
        print(f'OK: {OUTPUT_PATH} is in sync '
              f'({len(inv["builtin"])} built-in tools)')
        return 0

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(rendered)
    print(f'Wrote {OUTPUT_PATH} ({len(inv["builtin"])} built-in, '
          f'{len(inv["plugin"])} plugin)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
