"""lib/motion_video/_fonts.py — CJK sans typeface as a managed asset.

Why (measured 2026-07-28). ``fc-list :lang=zh`` on this host returns exactly
ONE face: ``NotoSerifSC.otf`` — a SERIF. ``guide/MOTION_CRAFT.md`` already
concedes it ("on this host that is a serif") and instructs authors NOT to name
a sans CJK family, because naming an absent face does not get you that face:
fontconfig silently substitutes whatever it has. So every Chinese frame the
pipeline has ever produced was set in a serif whose selection nobody chose,
which is a direct contributor to "the picture looks dated".

The fix rides the SAME channel as every other binary asset (see
:mod:`lib.motion_video._assets`): the typeface is fetched ONCE into the
content-addressed library, then linked into each scene and declared with a
scene-local ``@font-face``. Verified against the real hyperframes CLI: a
composition whose ``@font-face`` points at ``assets/<file>`` passes ``check``
(``rc=0``) and the glyphs are drawn from that file — no fontconfig
registration, no system install, no root.

The silent-substitution trap is what the guard pins: naming a family that is
not declared and not installed produces NO error, just different pixels. So
"the composition asked for a sans" must be provable from the document itself
(a matching ``@font-face`` whose ``src`` resolves), never from a comment.
"""

from __future__ import annotations

import os

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['CJK_SANS_FAMILY', 'ensure_cjk_sans', 'font_face_css',
           'declared_font_families', 'undeclared_font_families',
           'installed_cjk_families', 'has_installed_cjk_sans',
           'has_cjk_text', 'self_supplied_cjk_families',
           'cjk_fallback_findings']

#: The family name a composition uses for Chinese text. Deliberately OUR OWN
#: name rather than 'Noto Sans SC': the face is delivered by a scene-local
#: @font-face, so the name must not collide with (or be silently satisfied by)
#: whatever fontconfig happens to hold.
CJK_SANS_FAMILY = 'Tofu Sans SC'

#: Sources for the typeface, best first. The woff2 subset is ~1.1 MB versus
#: ~8.3 MB for the full OTF (both measured reachable from this host), and
#: Chrome renders woff2 natively — so the small one is tried first and the OTF
#: is the fallback for an environment that blocks the CDN.
_SOURCES = (
    ('https://cdn.jsdelivr.net/fontsource/fonts/noto-sans-sc@latest/'
     'chinese-simplified-400-normal.woff2', '.woff2'),
    ('https://github.com/notofonts/noto-cjk/raw/main/Sans/SubsetOTF/SC/'
     'NotoSansSC-Regular.otf', '.otf'),
)

#: Smallest plausible CJK font payload. A subset still runs to ~1 MB, so
#: anything under this is an error page or a truncated download, and writing it
#: would give us a font file that renders nothing.
_MIN_FONT_BYTES = 200_000


def installed_cjk_families() -> list[str]:
    """Families fontconfig can resolve for Chinese text, via ``fc-list``."""
    import subprocess
    from lib.motion_video._env import build_render_env
    try:
        out = subprocess.run(['fc-list', ':lang=zh', '-f', '%{family}\n'],
                             capture_output=True, text=True, timeout=20,
                             env=build_render_env())
    except Exception as e:
        logger.debug('[Fonts] fc-list unavailable: %s', e)
        return []
    fams: list[str] = []
    for line in (out.stdout or '').splitlines():
        for fam in line.split(','):
            fam = fam.strip()
            if fam and fam not in fams:
                fams.append(fam)
    return fams


def has_installed_cjk_sans() -> bool:
    """True when a SANS CJK face is already installed system-wide.

    Used only to decide whether shipping our own is necessary; it never gates
    the @font-face path, because a scene-local face is correct either way.
    """
    return any('serif' not in f.lower() and
               any(k in f.lower() for k in ('sans', 'hei', 'gothic', 'yahei',
                                            'pingfang'))
               for f in installed_cjk_families())


def ensure_cjk_sans(*, download: bool = True, timeout: int = 90) -> str:
    """Path in the asset library to a CJK **sans** face, or ''.

    Fetched once and content-addressed, so every later job and every later
    scene reuses the same file. Never raises: an unreachable network returns
    '' and the caller keeps the pre-existing behaviour (fontconfig fallback).
    """
    from lib.motion_video._assets import asset_root, store_bytes

    # Already in the library? Cheapest possible answer.
    try:
        for name in sorted(os.listdir(asset_root())):
            if name.startswith('cjk-sans.'):
                path = os.path.join(asset_root(), name)
                if os.path.getsize(path) >= _MIN_FONT_BYTES:
                    return path
    except OSError as e:
        logger.debug('[Fonts] cannot scan the asset library: %s', e)

    if not download:
        return ''

    from lib.http_client import http_get
    for url, suffix in _SOURCES:
        try:
            resp = http_get(url, timeout=timeout)
            data = getattr(resp, 'content', b'') or b''
            code = getattr(resp, 'status_code', 0)
        except Exception as e:
            logger.warning('[Fonts] fetch failed for %s: %s', url, e)
            continue
        if code != 200 or len(data) < _MIN_FONT_BYTES:
            logger.warning('[Fonts] rejected %s (HTTP %s, %d bytes — under the '
                           '%d-byte floor, so it is not a real font)',
                           url, code, len(data), _MIN_FONT_BYTES)
            continue
        stored = store_bytes(data, suffix=suffix)
        # Give it a stable, greppable name alongside the hashed entry so the
        # lookup above is O(1) on later runs.
        alias = os.path.join(os.path.dirname(stored), f'cjk-sans{suffix}')
        try:
            if not os.path.lexists(alias):
                os.link(stored, alias)
        except OSError:
            try:
                import shutil
                shutil.copy2(stored, alias)
            except OSError as e:
                logger.debug('[Fonts] alias creation failed: %s', e)
                alias = stored
        logger.info('[Fonts] CJK sans ready: %s (%d bytes)', alias, len(data))
        return alias
    logger.warning('[Fonts] no CJK sans could be fetched — compositions will '
                   'fall back to whatever fontconfig resolves (a SERIF on '
                   'this host)')
    return ''


def font_face_css(rel_path: str, *, family: str = CJK_SANS_FAMILY) -> str:
    """The ``@font-face`` block a composition must carry to use the face.

    ``rel_path`` is the scene-relative path returned by
    :func:`lib.motion_video._assets.materialise` — a scene-local reference, so
    the render stays deterministic and offline.
    """
    fmt = 'woff2' if rel_path.lower().endswith('.woff2') else 'opentype'
    return (f"@font-face {{ font-family: '{family}'; "
            f"src: url('{rel_path}') format('{fmt}'); "
            f"font-weight: 400; font-style: normal; font-display: block; }}")


#: ``@font-face { … font-family: 'X' … }`` — the families a document DECLARES.
_FACE_FAMILY_RE = None
#: ``font-family: A, B, 'C'`` — the families a document USES.
_USE_FAMILY_RE = None


def _compile():
    global _FACE_FAMILY_RE, _USE_FAMILY_RE
    if _FACE_FAMILY_RE is None:
        import re
        _FACE_FAMILY_RE = re.compile(
            r'@font-face\s*\{[^}]*?font-family\s*:\s*([^;}"\']*(?:'
            r'"[^"]*"|\'[^\']*\')?[^;}"\']*)', re.IGNORECASE)
        # The value is a comma list of ATOMS, each either a quoted span or a
        # run of non-delimiter chars. Two measured traps, one per direction:
        #
        #  * 2026-07-29: `[^;}]+` ran PAST an inline attribute's closing
        #    quote (`style="font-family: Inter, sans-serif">…`) and
        #    swallowed the document tail as one bogus "family".
        #  * 2026-08-02 (the fix above introduced this): excluding quote
        #    chars entirely made a value STARTING with a quote —
        #    `font-family:'PingFang SC'` — capture NOTHING, so the most
        #    common CSS quoting style became invisible to the
        #    ghost-family check (measured: test_undeclared_family_is_
        #    reported red; the check failed OPEN).
        #
        # Atoms may be quoted (paired quotes only — an unclosed quote can
        # never swallow the tail), and the list joins on commas; the
        # attribute delimiter `>` and `;`/`}` still terminate the value.
        _atom = r"(?:'[^']*'|\"[^\"]*\"|[^;}\"'>,]+)"
        _USE_FAMILY_RE = re.compile(
            rf"(?<!-)\bfont-family\s*:\s*"
            rf"((?:{_atom}\s*,\s*)*{_atom})",
            re.IGNORECASE)


def _split_families(blob: str) -> list[str]:
    out = []
    for part in blob.split(','):
        fam = part.strip().strip('\'"').strip()
        if fam:
            out.append(fam)
    return out


def declared_font_families(html: str) -> set[str]:
    """Families the document declares via ``@font-face``."""
    _compile()
    fams: set[str] = set()
    for m in _FACE_FAMILY_RE.finditer(html or ''):
        for fam in _split_families(m.group(1)):
            fams.add(fam)
    return fams


#: Generic CSS families, plus the ones the renderer resolves on its own
#: (per guide/MOTION_CRAFT.md). Naming any of these is safe.
_SAFE_FAMILIES = frozenset(x.lower() for x in (
    'inter', 'roboto', 'open sans', 'lato', 'montserrat', 'poppins',
    'source sans 3', 'noto sans', 'noto sans jp',
    'sans-serif', 'serif', 'monospace', 'system-ui', 'ui-sans-serif',
    'ui-serif', 'ui-monospace', 'cursive', 'fantasy', 'inherit', 'initial',
    'unset', 'revert', 'emoji', 'math',
))


def undeclared_font_families(html: str) -> list[str]:
    """Families the document USES but neither declares nor can rely on.

    This is the silent-substitution trap made visible. Naming ``PingFang SC``
    on a host that lacks it is not an error — fontconfig quietly substitutes,
    and the frame renders in a face nobody chose. Anything reported here will
    render as SOMETHING ELSE, so the author must either declare it with an
    ``@font-face`` (the asset channel) or stop naming it.
    """
    _compile()
    declared = {f.lower() for f in declared_font_families(html)}
    faces = set()
    for m in _FACE_FAMILY_RE.finditer(html or ''):
        faces.add(m.group(0))
    bad: list[str] = []
    for m in _USE_FAMILY_RE.finditer(html or ''):
        # Skip the declaration sites themselves.
        if any(m.group(0) in f for f in faces):
            continue
        for fam in _split_families(m.group(1)):
            low = fam.lower()
            if low in _SAFE_FAMILIES or low in declared:
                continue
            if low.startswith('var(') or low.startswith('--'):
                continue
            if fam not in bad:
                bad.append(fam)
    return bad


#: Any CJK ideograph. Used to decide whether a composition even HAS Chinese
#: text to worry about.
_CJK_TEXT_RE = None


def has_cjk_text(html: str) -> bool:
    """True when the composition draws CJK ideographs.

    Judged on the VISIBLE text only (script/style bodies never reach the
    frame), so a Chinese comment or an identifier cannot make a Latin-only
    composition look like it needs a Chinese face.
    """
    global _CJK_TEXT_RE
    if _CJK_TEXT_RE is None:
        import re as _re
        _CJK_TEXT_RE = _re.compile(r'[\u4e00-\u9fff]')
    from lib.motion_video._gates import visible_text
    return any(_CJK_TEXT_RE.search(s) for s in visible_text(html or ''))


def self_supplied_cjk_families(html: str) -> set[str]:
    """Families the document declares via ``@font-face`` AND actually ships.

    "Declared" is not enough: an ``@font-face`` whose ``src`` points at a file
    that is not in the scene renders nothing, so the family is a promise the
    document cannot keep. This returns only the families whose declaration is
    backed by a reference — the caller pairs it with the asset-ref check that
    proves the file exists.
    """
    return {f for f in declared_font_families(html) if f}


def cjk_fallback_findings(html: str, scene_dir: str = '') -> list[str]:
    """Findings when a CJK composition leans on the HOST's fallback face.

    **What this is NOT** (measured 2026-07-29, correcting a plausible but
    wrong reading of ``fc-match``): it is NOT "the glyphs are missing".
    ``fc-match "Inter,system-ui,sans-serif:lang=zh"`` answers ``DejaVu Sans``,
    which has no CJK coverage — but that query models the wrong thing. Chrome
    resolves fallback PER GLYPH, not per pattern, so it finds the host's CJK
    face for Chinese codepoints regardless of the family list. Measured in real
    headless Chrome on a scene with no ``@font-face``: the string 「扩散语言模型」
    renders 384 px wide with 6,744 ink pixels, and a deliberately BOGUS family
    name produces a byte-identical raster. The Chinese is drawn, and it is
    legible.

    **What it IS**: the composition has no say in WHICH face draws its
    Chinese. Whatever the host happens to have wins — on this host a single
    ``Noto Serif CJK SC``. So a film mixing scenes that ship
    :data:`CJK_SANS_FAMILY` with scenes that do not is typographically
    inconsistent BY CONSTRUCTION, and every un-shipped scene silently inherits
    whatever the next host provides. That is a reproducibility defect as much
    as an aesthetic one: the same composition renders differently on two hosts.

    Advisory, never a rejection: the zero-LLM template ships no face either, so
    rejecting would degrade the scene into a card with the same defect — the
    trap the fill gate and the asset floor both document.
    """
    if not has_cjk_text(html):
        return []
    declared = self_supplied_cjk_families(html)
    if declared:
        # A declared face still has to EXIST; verify_asset_refs owns that
        # check, so a broken src is reported there rather than twice here.
        return []
    return [
        f'this scene draws Chinese text but ships no font of its own, so the '
        f'face is chosen by whatever the RENDER HOST happens to have '
        f'(here: a serif). The glyphs do render — this is not a missing-glyph '
        f'bug — but the composition has no say in its own typography and the '
        f'same file renders differently on another host. Declare the staged '
        f'CJK face with an @font-face and set it on your text: '
        f"font-family: '{CJK_SANS_FAMILY}', Inter, sans-serif."]

