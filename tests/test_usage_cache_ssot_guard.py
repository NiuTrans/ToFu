#!/usr/bin/env python3
"""Static SSOT guard: no module under lib/ may read cache-usage alias keys
off a dict directly — every consumer MUST go through
``lib.cost.normalize_usage`` (or receive a dict already canonicalised by
``lib.cost.canonicalize_usage_cache_keys``).

WHY (the bug this walls off, 2026-07-24): kimi-k3 cache hits were invisible
for weeks because the gateway reports them as ``cached_tokens`` while pinning
``cache_read_tokens=0`` — and TWELVE separate call sites each hand-read only
the two canonical spellings. Every one was blind: accounting showed 0 hits
and over-billed at full input price. The fix (commit 146a872b) centralised
all alias knowledge in ``normalize_usage`` — but nothing stops the next
feature from adding a 13th direct read and silently re-growing the same
blind spot. This guard is that ratchet, modelled on
tests/test_frontend_api_isolation.py.

Scanned shapes (value-extraction only — writes / construction are fine):
  * ``d['cache_read_tokens']``           (Subscript in Load context)
  * ``d.get('cache_read_tokens', ...)``  (.get / .pop / .setdefault)

Known-clean exceptions live in ``_ALLOW`` with a justification each; the
list may only SHRINK (enforced by test_allowlist_entries_still_needed).
If you are reading a *pricing-rate table* or a *normalised accumulator*
through a constant-string read, add your file here with a reason — if you
are reading a raw provider usage dict, route through normalize_usage
instead.

Known limitation (accepted, ratchet-style): reads through a variable key
(``for k in KEYS: u.get(k)``) are invisible to a constant-string scanner;
lib/tasks_pkg/compaction/_compaction_usage.py legitimately sums that way
(and its consumers normalise downstream). The guard exists to stop the
casual copy-paste regression, not deliberate circumvention.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The full cache-channel alias universe known to lib/cost.py
# (_USAGE_KEY_ALIASES + _USAGE_NESTED_ALIASES): canonical OpenAI/Anthropic
# spellings plus every probed vendor variant (Kimi/Moonshot, DeepSeek,
# Gemini, sankuai-gateway camelCase).
_FORBIDDEN_KEYS = frozenset({
    'cache_read_tokens', 'cache_read_input_tokens',
    'cache_write_tokens', 'cache_creation_input_tokens',
    'cached_tokens', 'effectiveCachedTokens',
    'prompt_cache_hit_tokens', 'cached_content_token_count',
})

_ALLOW = {
    'lib/cost.py':
        'the SSOT itself — normalize_usage + canonicalize_usage_cache_keys',
    'lib/llm/anthropic_outbound/_from_anthropic.py':
        'Anthropic→OpenAI protocol translator — only Anthropic spellings '
        'exist on that wire by construction',
    'lib/paper/report_engine/_meta.py':
        "reads the engine's OWN normalised accumulator (usage_total is fed "
        'exclusively from normalize_usage in report_engine/__init__'
        '._accumulate_usage)',
}


def _find_direct_reads(source: str) -> list[str]:
    """Return ['L<n>: <kind>', ...] for every constant-string read of a
    forbidden cache-alias key in ``source``."""
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Load)
                and isinstance(node.slice, ast.Constant)
                and node.slice.value in _FORBIDDEN_KEYS):
            hits.append(f'L{node.lineno}: subscript[{node.slice.value!r}]')
        elif (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ('get', 'pop', 'setdefault')
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in _FORBIDDEN_KEYS):
            hits.append(
                f'L{node.lineno}: .{node.func.attr}({node.args[0].value!r})')
    return hits


class UsageCacheSsotGuardTest(unittest.TestCase):

    def test_no_direct_cache_key_reads_outside_allowlist(self):
        violations = []
        for root, dirs, files in os.walk(os.path.join(ROOT, 'lib')):
            # Prune caches AND dot-dirs: lib/.project_sessions/ holds one
            # shadow.git per session (tens of thousands of object files over
            # FUSE) — descending there hangs the walk for minutes. No legit
            # Python package dir starts with '.'.
            dirs[:] = [d for d in dirs
                       if d != '__pycache__' and not d.startswith('.')]
            for fname in sorted(files):
                if not fname.endswith('.py'):
                    continue
                path = os.path.join(root, fname)
                rel = os.path.relpath(path, ROOT)
                with open(path, encoding='utf-8') as fh:
                    src = fh.read()
                # Cheap pre-filter: skip files that mention no alias at all
                if not any(k in src for k in _FORBIDDEN_KEYS):
                    continue
                if rel in _ALLOW:
                    continue
                try:
                    hits = _find_direct_reads(src)
                except SyntaxError as e:
                    self.fail(f'{rel}: unparseable ({e})')
                violations.extend(f'{rel} — {h}' for h in hits)
        self.assertEqual(
            violations, [],
            'direct cache-alias reads outside lib/cost.py — route these '
            'through lib.cost.normalize_usage instead:\n  '
            + '\n  '.join(violations))

    def test_allowlist_entries_point_at_real_files(self):
        """An entry naming a renamed/deleted file hides that its
        justification went stale — keep the list honest."""
        for rel in _ALLOW:
            self.assertTrue(os.path.isfile(os.path.join(ROOT, rel)),
                            f'stale allowlist entry: {rel}')

    def test_allowlist_entries_still_needed(self):
        """Ratchet hygiene: once a file migrates to normalize_usage, its
        entry MUST be removed — the allowlist may only shrink."""
        for rel in _ALLOW:
            with open(os.path.join(ROOT, rel), encoding='utf-8') as fh:
                src = fh.read()
            self.assertTrue(
                _find_direct_reads(src),
                f'{rel} no longer reads cache aliases directly — remove its '
                f'allowlist entry')

    def test_scanner_catches_a_fresh_direct_read(self):
        """NEUTER: the exact regression shape (a fresh direct .get +
        subscript) MUST be flagged — proves the guard has teeth."""
        src = (
            'def cost_preview(usage):\n'
            "    return (usage.get('cache_read_tokens', 0)\n"
            "            + usage['cache_creation_input_tokens'])\n"
        )
        hits = _find_direct_reads(src)
        self.assertEqual(len(hits), 2, hits)

    def test_scanner_passes_writes_and_normalized_reads(self):
        """Construction, keyword args, Store-context writes and the
        canonical normalize_usage path are NOT violations."""
        src = (
            'from lib.cost import normalize_usage\n'
            'def f(u):\n'
            "    u['cache_read_tokens'] = 3\n"
            "    payload = {'cache_read_tokens': 3}\n"
            '    g(cache_read_tokens=3)\n'
            "    u['cache_read_tokens'] += 1\n"
            "    return normalize_usage(u)['cache_read'] + payload\n"
        )
        self.assertEqual(_find_direct_reads(src), [])


if __name__ == '__main__':
    unittest.main()
