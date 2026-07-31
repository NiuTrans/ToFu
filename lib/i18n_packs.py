"""lib/i18n_packs.py — build-time extraction of single-language i18n packs.

WHY THIS EXISTS
---------------
Epic-E sub-part 1 ships ONE dictionary carrying both zh and en (measured:
344.0 KB raw -> 89.8 KB brotli; a zh-only variant is 59.2 KB, i.e. 30.6 KB =
7.6% of the compressed first-paint payload — tests/test_i18n_split_sizing.py).
Slice 1 (landed) made the language server-visible via a cookie. This module is
the ATOM slice 2 needs: turning the one dual-language source into two
single-language packs, provably without losing or altering a single string.

WHY NODE AND NOT A REGEX
------------------------
The obvious implementation — regex the ``{ zh: '…', en: '…' }`` literals — is
wrong, and quietly so. The dictionary contains apostrophes, escaped quotes,
braces inside interpolation placeholders, and 378 comment lines. A regex that
is 99% right silently drops the 1% it cannot parse, and a MISSING key does not
throw: ``t()`` falls back to zh (see i18n.js), so an English UI would just
render Chinese for the dropped keys. That is the exact no-failure-signal
defect class this epic already fixed once at the ``t()`` level.

So the extraction EXECUTES the real file in node and reads the resulting
object. Whatever JavaScript thinks ``_i18n`` is, that is what we serialize —
the parser can never disagree with the runtime.

WHAT THIS MODULE DOES *NOT* DO
------------------------------
It does not touch what is served. Nothing here is wired into
``build_bundle()`` yet. Slice 2 does that, and it must not start until this
extraction is proven exact, because a pack that silently drops keys is
invisible in production.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = [
    'SUPPORTED_LANGS',
    'PACK_BASENAME_RE',
    'extract_dictionary',
    'build_pack_source',
    'verify_pack_roundtrip',
    'emit_pack_files',
]

# Kept in lockstep with routes/common.py::_UI_LANGS. A pack is only ever
# emitted for a language the server is willing to select.
SUPPORTED_LANGS = ('zh', 'en')

# The content-hashed artifact name pattern for emitted packs. Kept in lockstep
# with lib/js_bundler.py::_BUILT_BUNDLE_RE and
# tests/test_bundle_manifest_parity.py::_BUILT_BUNDLE_RE — the parity test's
# disk-orphan edge treats anything NOT matching its built-artifact regex as a
# source file, so a pack name outside this pattern trips the closed system.
PACK_BASENAME_RE = re.compile(r'^i18n-(?:zh|en)-[0-9a-f]{8}\.js$')

# How long a published pack is immune to cleanup, mirroring
# lib/js_bundler._BUILT_ARTIFACT_GRACE_S. Read from the SAME env var rather
# than imported, because js_bundler imports THIS module (importing back would
# be a cycle) — the shared var name is what keeps them in lockstep.
_ARTIFACT_GRACE_S = int(
    os.environ.get('TOFU_BUNDLE_ARTIFACT_GRACE_S', '7200'))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_SOURCE = os.path.join(_REPO, 'static', 'js', 'i18n.js')

# Minimal browser surface i18n.js touches at load: it reads localStorage for
# the language, writes a cookie, and _applyI18n walks the DOM. Stubs are inert
# so loading the file has no side effects beyond defining _i18n.
_DOM_STUB = """
globalThis.window = globalThis;
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.document = {
  documentElement: {},
  querySelectorAll: () => [],
  addEventListener: () => {},
  readyState: 'complete',
  get cookie() { return ''; },
  set cookie(v) {},
};
"""


class PackExtractionError(RuntimeError):
    """Raised when the dictionary cannot be extracted or fails verification."""


def _node() -> str:
    exe = shutil.which('node')
    if not exe:
        raise PackExtractionError('node is required to extract i18n packs')
    return exe


def _run_node(script: str, timeout: int = 120) -> str:
    with tempfile.NamedTemporaryFile('w', suffix='.js', delete=False,
                                     encoding='utf-8') as fh:
        fh.write(script)
        path = fh.name
    try:
        proc = subprocess.run([_node(), path], capture_output=True,
                              text=True, timeout=timeout)
    finally:
        try:
            os.unlink(path)
        except OSError as e:
            logger.debug('[i18nPacks] temp cleanup failed: %s', e)
    if proc.returncode != 0:
        raise PackExtractionError(f'node failed: {proc.stderr[:800]}')
    return proc.stdout


def extract_dictionary(source_path: str | None = None) -> dict:
    """Return the real ``_i18n`` object by EXECUTING i18n.js under node.

    Args:
        source_path: Override for the i18n.js path (tests).

    Returns:
        ``{key: {lang: text, ...}, ...}`` exactly as JavaScript sees it.

    Raises:
        PackExtractionError: node missing, file unreadable, or output not JSON.
    """
    path = source_path or I18N_SOURCE
    with open(path, encoding='utf-8') as f:
        src = f.read()

    out = _run_node(_DOM_STUB + src + "\nconsole.log('@@' + JSON.stringify(_i18n));\n")
    marker = [l for l in out.splitlines() if l.startswith('@@')]
    if not marker:
        raise PackExtractionError(f'no dictionary emitted; stdout tail: {out[-300:]}')
    try:
        data = json.loads(marker[-1][2:])
    except json.JSONDecodeError as e:
        raise PackExtractionError(f'dictionary is not valid JSON: {e}') from e
    if not isinstance(data, dict) or not data:
        raise PackExtractionError('extracted dictionary is empty or not an object')
    return data


def build_pack_source(dictionary: dict, lang: str) -> str:
    """Render a single-language pack as JavaScript source.

    The entry SHAPE is preserved — ``{key: {lang: text}}``, not
    ``{key: text}`` — so ``t()``'s ``entry[_i18nLang]`` lookup keeps working
    against a pack with zero changes to the accessor.

    Keys whose entry lacks *lang* are OMITTED rather than filled from another
    language. Omitting is the honest representation of "this pack does not
    have it", and ``t()``'s tripwire (``_reportMissingTranslation``, landed in
    4fbab4fa) makes that omission audible at runtime. Filling silently would
    reintroduce exactly the defect this epic removed.

    Args:
        dictionary: Output of :func:`extract_dictionary`.
        lang: One of :data:`SUPPORTED_LANGS`.

    Returns:
        JavaScript source declaring ``var _i18n = { … };``.
    """
    if lang not in SUPPORTED_LANGS:
        raise PackExtractionError(f'unsupported language {lang!r}')

    lines = [
        f'/* GENERATED — single-language i18n pack ({lang}). Do not edit.',
        ' * Source: static/js/i18n.js (extracted by lib/i18n_packs.py).',
        " * Entry shape is preserved as {key: {lang: text}} so t()'s",
        ' * entry[_i18nLang] lookup is unchanged. Keys missing this language',
        ' * are OMITTED, never filled from another language — the runtime',
        ' * tripwire reports them instead of silently rendering the wrong one.',
        ' */',
        'var _i18n = {',
    ]
    omitted = 0
    for key in dictionary:  # insertion order == source order
        entry = dictionary[key]
        if not isinstance(entry, dict) or lang not in entry:
            omitted += 1
            continue
        lines.append(f'{json.dumps(key)}:{{{lang}:{json.dumps(entry[lang], ensure_ascii=False)}}},')
    lines.append('};')
    if omitted:
        logger.info('[i18nPacks] %s pack omits %d key(s) lacking that language',
                    lang, omitted)
    return '\n'.join(lines) + '\n'


def verify_pack_roundtrip(dictionary: dict, pack_src: str, lang: str) -> dict:
    """Prove a rendered pack reproduces the source strings EXACTLY.

    Executes the generated pack under node and compares every key/value
    against *dictionary*. This is the gate that must pass before slice 2 may
    serve a pack, because the failure mode it guards (a dropped key) produces
    no runtime error — only the wrong language on screen.

    Returns:
        ``{'keys': int, 'missing': [...], 'mismatched': [...], 'extra': [...]}``
        with all three lists empty on success.

    Raises:
        PackExtractionError: the pack is not valid JS / emits no object.
    """
    # _DOM_STUB first: a FULL pack (drop-in i18n.js replacement) runs
    # module-level code — reads localStorage, writes document.cookie — at
    # load. The stub is inert and changes nothing for dict-only packs.
    out = _run_node(_DOM_STUB + pack_src + "\nconsole.log('@@' + JSON.stringify(_i18n));\n")
    marker = [l for l in out.splitlines() if l.startswith('@@')]
    if not marker:
        raise PackExtractionError('generated pack emitted no dictionary')
    got = json.loads(marker[-1][2:])

    expected = {k: v[lang] for k, v in dictionary.items()
                if isinstance(v, dict) and lang in v}

    missing = [k for k in expected if k not in got]
    extra = [k for k in got if k not in expected]
    mismatched = [
        k for k in expected
        if k in got and got[k].get(lang) != expected[k]
    ]
    return {
        'keys': len(expected),
        'missing': missing,
        'mismatched': mismatched,
        'extra': extra,
    }

def build_full_pack_source(i18n_source: str, dictionary: dict, lang: str) -> str:
    """Render a FULL per-language i18n.js: all functions + single-language dict.

    The pack is a drop-in REPLACEMENT for i18n.js, not a dictionary fragment —
    it carries ``t()`` / ``setLanguage`` / ``_applyI18n`` / the tripwire / the
    cookie mirror, with ONLY the ``var _i18n = {…}`` block swapped for the
    single-language dictionary. That is what lets the core bundle exclude
    i18n.js entirely while the server injects exactly one pack tag in its
    place, and what lets the same file serve as the on-demand fetch target
    when ``setLanguage()`` switches to the other language.

    Boundary derivation (verified, not assumed): the dictionary block starts
    at a line beginning exactly ``var _i18n = {`` and ends at the FIRST
    column-0 ``};`` line after it. Column-0 ``};`` cannot occur inside the
    block because every entry line is indented. Both boundaries are re-checked
    here and the block is asserted to EXECUTE to *dictionary* — a drifted
    boundary raises instead of silently emitting a truncated pack.

    Args:
        i18n_source: Raw text of static/js/i18n.js.
        dictionary: Output of :func:`extract_dictionary` (used to render the
            replacement block AND to sanity-check the located block).
        lang: One of :data:`SUPPORTED_LANGS`.

    Returns:
        Complete JavaScript source of the per-language i18n.js.

    Raises:
        PackExtractionError: boundaries not found / not unique / the located
            block does not evaluate to the extracted dictionary.
    """
    lines = i18n_source.splitlines(keepends=True)
    starts = [i for i, l in enumerate(lines) if l.startswith('var _i18n = {')]
    if len(starts) != 1:
        raise PackExtractionError(
            f'expected exactly one "var _i18n = {{" line-start, found {len(starts)}')
    start = starts[0]
    ends = [i for i in range(start + 1, len(lines)) if lines[i].rstrip('\n') == '};']
    if not ends:
        raise PackExtractionError('no column-0 "};" found after dictionary start')
    end = ends[0]

    # The block must BE the dictionary — evaluate it and compare key sets.
    # Cheap and decisive: if the boundary drifted onto some other block, the
    # key sets diverge immediately.
    block = ''.join(lines[start:end + 1])
    probe = _run_node(block + "\nconsole.log('@@' + JSON.stringify(Object.keys(_i18n)));\n")
    marker = [l for l in probe.splitlines() if l.startswith('@@')]
    if not marker:
        raise PackExtractionError('located dictionary block does not execute')
    block_keys = set(json.loads(marker[-1][2:]))
    if block_keys != set(dictionary):
        raise PackExtractionError(
            f'located block key set diverges from extracted dictionary '
            f'(block={len(block_keys)} vs dict={len(dictionary)}) — boundary drift')

    replacement = build_pack_source(dictionary, lang)
    return ''.join(lines[:start]) + replacement + ''.join(lines[end + 1:])


def _node_syntax_ok(path: str) -> tuple[bool, str]:
    """node --check a built artifact. Fail-CLOSED: a syntactically broken pack
    must never be published (it would blank t() for every module), and unlike
    the bundler's best-effort gate we REQUIRE node here — extraction already
    needed it, so by this point node is guaranteed present."""
    proc = subprocess.run([_node(), '--check', path],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout)[:500]
    return True, ''


def emit_pack_files(out_dir: str, source_path: str | None = None) -> dict[str, str]:
    """Extract → render → verify → publish one pack per supported language.

    Args:
        out_dir: Directory the artifacts land in (``static/js`` in production).
        source_path: Override for the i18n.js path (tests).

    Returns:
        ``{lang: filename}`` for every language in :data:`SUPPORTED_LANGS`,
        e.g. ``{'zh': 'i18n-zh-1a2b3c4d.js', 'en': 'i18n-en-5e6f7a8b.js'}``.

    Raises:
        PackExtractionError: extraction failed, a pack failed verification, or
            the syntax gate rejected an artifact. NOTHING is published on
            failure — the caller must fall back to serving the dual-language
            i18n.js (the status quo), never a partial pack set.

    Atomicity + staleness:
        Each artifact is written to a private temp file in *out_dir* and
        ``os.replace()``d into place, so a reader never sees a partial write.
        Stale pack artifacts (names matching :data:`PACK_BASENAME_RE` that are
        not in the current set) are removed AFTER all current packs publish —
        a failed build deletes nothing, so the last-good set stays servable.
    """
    path = source_path or I18N_SOURCE
    with open(path, encoding='utf-8') as f:
        i18n_text = f.read()
    dictionary = extract_dictionary(source_path)

    published: dict[str, str] = {}
    for lang in SUPPORTED_LANGS:
        src = build_full_pack_source(i18n_text, dictionary, lang)

        # Structural gate: the pack is a drop-in i18n.js REPLACEMENT, so the
        # functions the whole app calls must survive the block replacement.
        for fn in ('function t(', 'function setLanguage(',
                   'function _applyI18n(', '_reportMissingTranslation'):
            if fn not in src:
                raise PackExtractionError(
                    f'{lang} pack lost {fn!r} during dictionary replacement — '
                    f'the bundle excludes i18n.js, so the pack is the ONLY '
                    f'copy of these functions')

        # The load-bearing gate: the pack must reproduce the source strings
        # EXACTLY before it is allowed to exist on disk. A pack that drops a
        # key renders the wrong language with no runtime error (see module
        # docstring) — this is the only place that can catch it.
        check = verify_pack_roundtrip(dictionary, src, lang)
        if check['missing'] or check['mismatched'] or check['extra']:
            raise PackExtractionError(
                f'{lang} pack failed roundtrip: missing={check["missing"][:3]} '
                f'mismatched={check["mismatched"][:3]} extra={check["extra"][:3]}')

        digest = hashlib.sha256(src.encode('utf-8')).hexdigest()[:8]
        filename = f'i18n-{lang}-{digest}.js'
        final_path = os.path.join(out_dir, filename)

        # Content-hash short-circuit: an existing artifact of this exact name
        # is byte-identical — nothing to do (also makes concurrent emitters
        # converge instead of racing).
        if os.path.exists(final_path):
            published[lang] = filename
            continue

        fd, tmp_path = tempfile.mkstemp(prefix=f'.{filename}.', suffix='.js',
                                        dir=out_dir)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(src)
            ok, detail = _node_syntax_ok(tmp_path)
            if not ok:
                raise PackExtractionError(
                    f'{lang} pack failed node --check: {detail}')
            os.replace(tmp_path, final_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError as _e:
                logger.debug('emit pack files: unreadable (%s)', _e)
                pass
            raise
        published[lang] = filename

    # Clean stale packs only after every current pack is safely on disk, and
    # never one that is still YOUNG. A pack is referenced by an already-served
    # index.html; deleting it the instant a rebuild renames it 404s the page
    # that is loading RIGHT NOW. Unlike a bundle, a missing pack has no
    # user-visible error path — the core bundle excludes i18n.js, so t() and
    # _i18nLang are simply undefined and the UI renders raw keys. Same grace
    # window as the bundle cleaner (lib/js_bundler._BUILT_ARTIFACT_GRACE_S),
    # read from the same env var so the two can't drift apart.
    current = set(published.values())
    now = time.time()
    try:
        for f in os.listdir(out_dir):
            if not PACK_BASENAME_RE.match(f) or f in current:
                continue
            path = os.path.join(out_dir, f)
            try:
                if now - os.path.getmtime(path) < _ARTIFACT_GRACE_S:
                    continue
                os.remove(path)
            except OSError as e:
                logger.debug('[i18nPacks] stale pack cleanup failed %s: %s', f, e)
    except OSError as e:
        logger.debug('[i18nPacks] pack dir listing failed: %s', e)

    logger.info('[i18nPacks] emitted %s', published)
    return published
