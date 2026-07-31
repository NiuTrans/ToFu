"""lib/memory/storage/_frontmatter.py — YAML-like frontmatter parse/build.

Split out of the former monolithic ``lib/memory/storage.py``; the public
import surface is preserved by the package ``__init__`` re-exporting every
symbol. This module is a leaf (no intra-package imports).
"""

import json
import re

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════
#  Frontmatter Parsing
# ═══════════════════════════════════════════════════════

_FM_RE = re.compile(r'^---\s*\n(.*?)\n---\s*\n', re.DOTALL)

#: Keys whose value is a LIST by contract, so a bare ``a, b, c`` (no brackets)
#: must be split rather than kept as one string.
#:
#: Deliberately a narrow allow-list, not "split anything containing a comma":
#: ``description`` and ``name`` are free prose and routinely contain commas —
#: splitting those would corrupt real content, which is far worse than the
#: rendering bug this fixes. Add a key here only when its consumers genuinely
#: iterate it.
_COMMA_LIST_KEYS = frozenset({'tags', 'keywords'})


def _parse_frontmatter(text):
    """Parse YAML-like frontmatter from markdown text. Returns (meta_dict, body).

    Supports:
      - Single-line scalars: ``name: foo``
      - Booleans: ``enabled: true`` / ``yes`` / ``no``
      - Inline lists: ``tags: [a, b]``
      - Quoted strings: ``description: "..."``
      - YAML folded scalars (``description: >`` followed by indented continuation lines)
      - Single-line JSON object after a key: ``metadata: {"openclaw":{...}}``
        (used by Anthropic Skills / OpenClaw / mlp-skills packages)
      - Single-line JSON object spread across multiple indented lines under
        ``metadata:`` — collapsed and parsed as JSON.
    """
    m = _FM_RE.match(text)
    if not m:
        return {}, text

    fm_text = m.group(1)
    body = text[m.end():]
    meta = {}

    raw_lines = fm_text.split('\n')
    i = 0
    while i < len(raw_lines):
        raw = raw_lines[i]
        stripped = raw.strip()
        if not stripped or stripped.startswith('#'):
            i += 1
            continue
        if ':' not in raw:
            i += 1
            continue

        # Detect indentation of this top-level key — top-level keys have
        # zero leading whitespace; nested lines (e.g. metadata block body)
        # have leading whitespace.
        leading = len(raw) - len(raw.lstrip(' '))
        if leading > 0:
            i += 1
            continue

        key, _, val = raw.partition(':')
        key = key.strip()
        val = val.strip()

        # ── Case A: folded scalar (``key: >``) ─────────────────────────
        if val == '>' or val == '|':
            buf = []
            j = i + 1
            while j < len(raw_lines):
                nxt = raw_lines[j]
                if not nxt.strip():
                    j += 1
                    continue
                if not nxt.startswith((' ', '\t')):
                    break
                buf.append(nxt.strip())
                j += 1
            joined = ' '.join(buf) if val == '>' else '\n'.join(buf)
            meta[key] = joined
            i = j
            continue

        # ── Case B: JSON object (single- or multi-line) ────────────────
        if val.startswith('{'):
            buf = [val]
            depth = val.count('{') - val.count('}')
            j = i + 1
            while depth > 0 and j < len(raw_lines):
                nxt = raw_lines[j]
                buf.append(nxt.strip())
                depth += nxt.count('{') - nxt.count('}')
                j += 1
            joined = ' '.join(buf)
            try:
                meta[key] = json.loads(joined)
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug('Frontmatter JSON parse failed for key=%s: %s',
                             key, e)
                meta[key] = joined  # fall back to raw string
            i = j
            continue

        # ── Case C: scalar / list / boolean ───────────────────────────
        if val.lower() in ('true', 'yes'):
            meta[key] = True
        elif val.lower() in ('false', 'no'):
            meta[key] = False
        elif val.startswith('[') and val.endswith(']'):
            meta[key] = [v.strip().strip('"\'') for v in val[1:-1].split(',') if v.strip()]
        elif key in _COMMA_LIST_KEYS and ',' in val:
            # Bare comma list: ``tags: a, b, c`` (no brackets). Hand-written
            # memory files use this form freely, and _build_frontmatter only
            # ever EMITS the bracketed form — so a file written by hand (or by
            # an older writer) parsed to a plain STRING while the API contract
            # and every consumer expect list[str].
            #
            # Measured 2026-07-28 on the real corpus: 6 of 1163 tagged memory
            # files hit this, and each one crashed its card in the browser with
            # `TypeError: sk.tags.forEach is not a function` (memory.js:231),
            # rendering "memory-card-error" instead of the memory. Found by the
            # new browser JS-error capture, not by any assertion — nothing in
            # the suite was watching the console.
            #
            # Fixed HERE rather than by defensive coercion in the frontend:
            # this is the single parse seam, so /api/v1/memory/list, injection
            # and search all get list[str] too, instead of each growing its own
            # workaround for the same malformed field.
            meta[key] = [v.strip().strip('"\'') for v in val.split(',') if v.strip()]
        elif (val.startswith('"') and val.endswith('"')) or \
             (val.startswith("'") and val.endswith("'")):
            meta[key] = val[1:-1]
        else:
            meta[key] = val
        i += 1

    return meta, body


def _coerce_str_list(val):
    """Best-effort coerce ``val`` (str | list | None) to a list[str]."""
    if val is None or val == '':
        return []
    if isinstance(val, list):
        return [str(x) for x in val if x]
    return [str(val)]


def _extract_package_metadata(meta):
    """Extract `requires_bins` / `requires_env` / `homepage` / `always` /
    `os` from an Anthropic / OpenClaw-style ``metadata`` block.

    Recognises both ``metadata.openclaw`` and the legacy
    ``metadata.clawdbot`` layout.  Returns a dict with keys::

        requires_bins, requires_env, requires_any_bins,
        requires_os, homepage, always, primary_env, install_specs

    All keys are always present — values default to empty lists / None.
    """
    out = {
        'requires_bins': [],
        'requires_env': [],
        'requires_any_bins': [],
        'requires_os': [],
        'homepage': '',
        'always': False,
        'primary_env': '',
        'install_specs': [],
    }
    md = meta.get('metadata') if isinstance(meta, dict) else None
    if not isinstance(md, dict):
        return out

    block = md.get('openclaw') or md.get('clawdbot') or {}
    if not isinstance(block, dict):
        return out

    requires = block.get('requires') or {}
    if isinstance(requires, dict):
        out['requires_bins'] = _coerce_str_list(requires.get('bins'))
        out['requires_any_bins'] = _coerce_str_list(requires.get('anyBins'))
        out['requires_env'] = _coerce_str_list(requires.get('env'))

    out['requires_os'] = _coerce_str_list(block.get('os'))
    out['homepage'] = str(block.get('homepage') or meta.get('homepage') or '')
    out['always'] = bool(block.get('always'))
    out['primary_env'] = str(block.get('primaryEnv') or '')
    install = block.get('install')
    if isinstance(install, list):
        out['install_specs'] = install
    return out


def _build_frontmatter(meta):
    """Build YAML-like frontmatter string from dict."""
    lines = ['---']
    for key, val in meta.items():
        if isinstance(val, bool):
            lines.append(f'{key}: {"true" if val else "false"}')
        elif isinstance(val, list):
            inner = ', '.join(str(v) for v in val)
            lines.append(f'{key}: [{inner}]')
        else:
            lines.append(f'{key}: {val}')
    lines.append('---')
    return '\n'.join(lines) + '\n'
