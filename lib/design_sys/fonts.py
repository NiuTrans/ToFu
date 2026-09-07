"""lib/design_sys/fonts.py — the curated, license-vetted typeface registry.

Why a registry instead of "download a font when one is needed" (the
pre-2026-08 state in ``lib.motion_video._fonts``): a single hard-coded CJK
sans made every film and every deck look the same regardless of subject. The
design-system answer is a small AUDITED library — every face here was chosen
for a scenario, its bytes are pinned by SHA-256 (a truncated or substituted
download is refused, never rendered), and its license is recorded with the
official evidence URL. Fonts with unverifiable redistribution terms are
absent ON PURPOSE (核验不过的字体宁可缺位).

Channel notes (all URLs measured reachable 2026-08-06 from this host):

  * ``@fontpkg/*`` and ``@fontsource/*`` via cdn.jsdelivr.net/npm — single
    woff2/otf files, ideal for scene-local ``@font-face``.
  * ``cdn.jsdelivr.net/fontsource/fonts/...`` for the Noto CJK subsets.
  * ZCOOL WenYiTi has no npm package; the wordshub/free-font raw file is the
    pinned source.

The registry is DATA — adding a font is appending one FontFace row, never
touching logic.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['FontFace', 'FONT_REGISTRY', 'LICENSES', 'get_font',
           'resolve_font', 'canonical_font_family', 'list_fonts',
           'get_pairing', 'ensure_font', 'font_face_block',
           'stage_font_into_scene', 'registry_summary']

# ── Licenses ──────────────────────────────────────────────
#: Every face maps to one entry. ``url`` is the OFFICIAL statement of the
#: redistribution terms, re-checked when a face is added.
LICENSES = {
    'ofl-1.1': {
        'name': 'SIL Open Font License 1.1',
        'url': 'https://openfontlicense.org/',
        'note': 'Free for commercial use, embedding and modification; the '
                'font itself may not be sold on its own.',
    },
    'misans-free': {
        'name': 'MiSans 字体知识产权许可协议(免费商用)',
        'url': 'https://hyperos.mi.com/font/',
        'note': 'Xiaomi grants free commercial use; no resale of the font '
                'file itself, no trademark use.',
    },
    'alimama-free': {
        'name': '阿里妈妈字体授权声明(免费商用)',
        'url': 'https://fonts.alibabagroup.com/',
        'note': 'Alimama grants individuals and enterprises free commercial '
                'use of the font software.',
    },
    'zcool-free': {
        'name': '站酷字体免费商用声明',
        'url': 'https://www.zcool.com.cn/assets/ZNzg1Ng==',
        'note': '免费授权全社会使用(包括商用)。',
    },
}


@dataclass(frozen=True)
class FontSource:
    """One downloadable weight of a face. ``sha256``/``size`` are AUDIT PINS
    measured at registry time — :func:`ensure_font` refuses drifted bytes."""
    weight: int
    url: str
    sha256: str
    size: int
    fmt: str                                # 'woff2' | 'opentype' | 'truetype'


@dataclass(frozen=True)
class FontFace:
    id: str                                 # registry key, e.g. 'misans'
    family: str                             # CSS/OOXML family name (REAL name:
                                            # PowerPoint resolves it when the
                                            # user has the face installed)
    label: str                              # human label, zh preferred
    roles: tuple = ()                       # 'display' | 'body' | 'latin'
    scenarios: tuple = ()                   # scenario ids from themes.py
    sources: tuple = ()                     # FontSource per shipped weight
    license_id: str = ''
    fallback_stack: str = 'sans-serif'
    note: str = ''


_JD = 'https://cdn.jsdelivr.net'
_FP = f'{_JD}/npm/@fontpkg'
_FS = f'{_JD}/npm/@fontsource'
_FSO = f'{_JD}/fontsource/fonts'

FONT_REGISTRY: tuple = (
    FontFace(
        id='misans', family='MiSans', label='小米 MiSans',
        roles=('display', 'body'),
        scenarios=('tech-engineering', 'business-plan', 'management-report',
                   'analysis-decision'),
        sources=(
            FontSource(400, f'{_FP}/mi-sans@4.3.0/MiSans-Regular.otf',
                       '962c1755fbfe9e0ac9762c9ec954784dcf0beb8cc2731537abddf8371b164b9a',
                       6499984, 'opentype'),
            FontSource(600, f'{_FP}/mi-sans@4.3.0/MiSans-Demibold.otf',
                       '67824977c8be99f4d47cef03c7a45c22dc37047b8be158b24941d11c3ad56455',
                       6456744, 'opentype'),
            FontSource(700, f'{_FP}/mi-sans@4.3.0/MiSans-Bold.otf',
                       'bff025b049c83d614fdabfbee0e4c2e4569ff9956745e669a45da1b7a4ea09a8',
                       6573740, 'opentype'),
        ),
        license_id='misans-free',
        note='现代几何黑体,屏幕阅读极佳;科技/商务/汇报的默认首选。'),
    FontFace(
        id='noto-sans-sc', family='Noto Sans SC', label='思源黑体',
        roles=('body',),
        scenarios=('management-report', 'academic-research',
                   'education-training'),
        sources=(
            FontSource(400, f'{_FSO}/noto-sans-sc@latest/chinese-simplified-400-normal.woff2',
                       '95e3633b6a98f764ba3adfb54504a0cd4799328c009adf9081d6c1850f9c4c78',
                       1142552, 'woff2'),
            FontSource(700, f'{_FSO}/noto-sans-sc@latest/chinese-simplified-700-normal.woff2',
                       'e1df51edc00bce27b58044e829fb8ec6accc8a5daece475413de90d52818845c',
                       1172244, 'woff2'),
        ),
        license_id='ofl-1.1',
        note='中性通用正文黑体;因太常见,仅作 fallback 级正文使用。'),
    FontFace(
        id='noto-serif-sc', family='Noto Serif SC', label='思源宋体',
        roles=('display', 'body'),
        scenarios=('academic-research', 'brand-creative'),
        sources=(
            FontSource(400, f'{_FSO}/noto-serif-sc@latest/chinese-simplified-400-normal.woff2',
                       '7dd5aea2df4644e916c2eb558bc8ed6ad6d8925c2c8e251fe68f7206da211696',
                       1507260, 'woff2'),
            FontSource(700, f'{_FSO}/noto-serif-sc@latest/chinese-simplified-700-normal.woff2',
                       '7535a804cc83aa0e8f40fdb1170556ad54ea3260e087a368f3ad6ab4bc86ca4f',
                       1557548, 'woff2'),
        ),
        license_id='ofl-1.1',
        note='宋体衬线,文化/学术/正式场合的正文与标题。'),
    FontFace(
        id='smiley-sans', family='Smiley Sans', label='得意黑',
        roles=('display',),
        scenarios=('brand-creative', 'business-plan'),
        sources=(
            FontSource(400, f'{_FP}/smiley-sans@2.0.4/SmileySans-Oblique.otf.woff2',
                       '4895e7a5b72753b7d4bf090581fbc4375e0ec53484944f369a584588f1eeaf08',
                       1361268, 'woff2'),
        ),
        license_id='ofl-1.1',
        note='窄斜美术黑体,人文感+几何感;创意/品牌页标题(无正体,勿作正文)。'),
    FontFace(
        id='alimama-shuheiti', family='Alimama ShuHeiTi', label='阿里妈妈数黑体',
        roles=('display',),
        scenarios=('business-plan', 'management-report'),
        sources=(
            FontSource(700, f'{_FP}/alimama-shu-hei-ti@1.0.5/AlimamaShuHeiTi-Bold.woff2',
                       '736cfeb978bbdccd0404f0e56a561174cbff078133214254908e29655a9505e6',
                       594404, 'woff2'),
        ),
        license_id='alimama-free',
        note='几何粗黑,商业感强;电商/营销/商务大标题。'),
    FontFace(
        id='alimama-daoliti', family='Alimama DaoLiTi', label='阿里妈妈刀隶体',
        roles=('display',),
        scenarios=('brand-creative',),
        sources=(
            FontSource(400, f'{_FP}/alimama-dao-li-ti@1.0.5/AlimamaDaoLiTi.woff2',
                       '4435a6382d23ee14c7cbc24f9ea553dabe0046140e4dda95e2838c42744666a7',
                       3416988, 'woff2'),
        ),
        license_id='alimama-free',
        note='隶书刀锋,国潮/文化/艺术类封面与章节页标题。'),
    FontFace(
        id='lxgw-wenkai', family='LXGW WenKai', label='霞鹜文楷',
        roles=('body', 'display'),
        scenarios=('education-training', 'brand-creative'),
        sources=(
            FontSource(400, f'{_FS}/lxgw-wenkai@5.3.0/files/lxgw-wenkai-latin-300-normal.woff2',
                       '73311ce540f4ed6ca41325d28e48037acf0d78e08de0f9f10078cd17ec19eb0f',
                       8811960, 'woff2'),
            FontSource(700, f'{_FS}/lxgw-wenkai@5.3.0/files/lxgw-wenkai-latin-700-normal.woff2',
                       'dc30e882f78e63546ffdda0e0e5d32fd9e12d64aede873d266dea2f89ba39dce',
                       7485292, 'woff2'),
        ),
        license_id='ofl-1.1',
        note='温润楷体,教育/人文/轻叙事正文与标题。'),
    FontFace(
        id='zcool-wenyiti', family='ZCOOL WenYiTi', label='站酷文艺体',
        roles=('display', 'body'),
        scenarios=('brand-creative', 'education-training'),
        sources=(
            FontSource(400, 'https://raw.githubusercontent.com/wordshub/free-font/'
                            'master/assets/font/%E4%B8%AD%E6%96%87/'
                            '%E7%AB%99%E9%85%B7%E5%AD%97%E4%BD%93%E7%B3%BB%E5%88%97/'
                            '%E7%AB%99%E9%85%B7%E6%96%87%E8%89%BA%E4%BD%93.ttf',
                       '92f38e2c2cfbfe2760a26a4273c3505c1a1aec41aada725eeba416c15e131fbd',
                       3973176, 'truetype'),
        ),
        license_id='zcool-free',
        note='清新手写感文艺体;轻设计/生活方式题材。'),
    FontFace(
        id='liter', family='Liter', label='Liter',
        roles=('latin',),
        scenarios=('tech-engineering', 'analysis-decision'),
        sources=(
            FontSource(400, f'{_FSO}/liter@latest/latin-400-normal.woff2',
                       'fe2a35c3a43761865e0f5d72f44aa40c73de569c0d1990649060a59839d39677',
                       17688, 'woff2'),
        ),
        license_id='ofl-1.1',
        note='现代 neo-grotesque 拉丁体;科技/产品页的西文与数字。'),
    FontFace(
        id='oranienbaum', family='Oranienbaum', label='Oranienbaum',
        roles=('latin',),
        scenarios=('brand-creative',),
        sources=(
            FontSource(400, f'{_FSO}/oranienbaum@latest/latin-400-normal.woff2',
                       '8ab24c8b63edb5f7307d7eb83a0613bbfcec267700d781324d4462ce4ea897f6',
                       20504, 'woff2'),
        ),
        license_id='ofl-1.1',
        note='高对比现代衬线拉丁体;时尚/艺术题材的西文大标题。'),
)

_BY_ID = {f.id: f for f in FONT_REGISTRY}
_BY_FAMILY = {f.family.casefold(): f for f in FONT_REGISTRY}

# Names produced by common browser/system font stacks for the same registered
# face.  PPTX embedding is keyed by the exact run typeface, so leaving an alias
# unresolved creates a package that appears to contain fonts while Office still
# substitutes every aliased run.  Keep this table small and explicit: an alias
# is accepted only when its glyph source and redistribution license are the
# audited registry entry on the right.
_FAMILY_ALIASES = {
    'noto sans cjk sc': 'noto-sans-sc',
    'source han sans sc': 'noto-sans-sc',
    'source han sans cn': 'noto-sans-sc',
    'noto serif cjk sc': 'noto-serif-sc',
    'source han serif sc': 'noto-serif-sc',
}

#: Role-pairing shortcuts: scenario → (display_id, body_id, latin_id).
#: themes.py consumes this; it is data, not logic.
_PAIRINGS = {
    'tech-engineering':  ('misans', 'misans', 'liter'),
    'analysis-decision': ('misans', 'noto-sans-sc', 'liter'),
    'business-plan':     ('alimama-shuheiti', 'misans', 'liter'),
    'management-report': ('alimama-shuheiti', 'noto-sans-sc', 'liter'),
    'academic-research': ('noto-serif-sc', 'noto-serif-sc', 'oranienbaum'),
    'education-training': ('lxgw-wenkai', 'lxgw-wenkai', 'liter'),
    'brand-creative':    ('smiley-sans', 'zcool-wenyiti', 'oranienbaum'),
}

#: Sanity floor for a downloaded face — the smallest pinned file is a 17 KB
#: latin woff2, so anything under 8 KB is an error page, not a font.
_MIN_FONT_BYTES = 8192


def get_font(font_id: str) -> FontFace | None:
    return _BY_ID.get(font_id)


def resolve_font(value: str) -> FontFace | None:
    """Resolve a registry id, canonical family, or audited family alias."""
    key = str(value or '').strip()
    if not key:
        return None
    return (_BY_ID.get(key) or _BY_FAMILY.get(key.casefold())
            or _BY_ID.get(_FAMILY_ALIASES.get(key.casefold(), '')))


def canonical_font_family(value: str, *, fallback: str = '') -> str:
    """Return the OOXML/CSS family that matches the embedded font's names."""
    face = resolve_font(value)
    return face.family if face is not None else str(value or fallback)


def list_fonts(*, scenario: str = '', role: str = '') -> list:
    out = []
    for f in FONT_REGISTRY:
        if scenario and scenario not in f.scenarios:
            continue
        if role and role not in f.roles:
            continue
        out.append(f)
    return out


def get_pairing(scenario: str) -> tuple:
    """(display FontFace, body FontFace, latin FontFace) for a scenario.

    Falls back to the tech pairing (MiSans/Liter) — a film with an
    unclassifiable subject still gets ONE coherent type system instead of
    per-scene roulette.
    """
    ids = _PAIRINGS.get(scenario) or _PAIRINGS['tech-engineering']
    return tuple(_BY_ID[i] for i in ids)


def ensure_font(font_id: str, weight: int, *, download: bool = True,
                timeout: int = 120) -> str:
    """Path in the design asset library to one (face, weight), or ''.

    Downloaded ONCE into the content-addressed store; the SHA-256 pin is
    verified before storing — measured need: a parallel first-fetch truncated
    MiSans-Regular to 92% of its bytes, and only the pin caught it.
    """
    from lib.design_sys._store import AssetStoreError, store_bytes

    face = get_font(font_id)
    if face is None:
        logger.warning('[Fonts] unknown font id %r', font_id)
        return ''
    src = next((s for s in face.sources if s.weight == weight), None)
    if src is None:
        # Nearest available weight beats none (e.g. asking 500 of a face that
        # ships 400/700 only) — but never silently across a 300-unit gap.
        src = min(face.sources, key=lambda s: abs(s.weight - weight))
        if abs(src.weight - weight) > 200:
            logger.warning('[Fonts] %s has no weight near %s', font_id, weight)
            return ''

    lib_dir = os.path.join(_store_dir(), font_id)
    suffix = os.path.splitext(src.url.split('?')[0])[1] or '.woff2'
    cached = os.path.join(lib_dir, f'w{src.weight}{suffix}')
    if os.path.isfile(cached) and os.path.getsize(cached) == src.size:
        return cached

    if not download:
        return ''
    from lib.http_client import http_get
    try:
        resp = http_get(src.url, timeout=timeout)
        data = getattr(resp, 'content', b'') or b''
        code = getattr(resp, 'status_code', 0)
    except Exception as e:
        logger.warning('[Fonts] fetch failed for %s w%s: %s',
                       font_id, src.weight, e)
        return ''
    if code != 200 or len(data) < _MIN_FONT_BYTES:
        logger.warning('[Fonts] rejected %s w%s (HTTP %s, %d bytes)',
                       font_id, src.weight, code, len(data))
        return ''
    try:
        store_bytes(data, name=f'{font_id}-w{src.weight}{suffix}',
                    subdir=f'fonts/{font_id}', sha256=src.sha256)
    except AssetStoreError as e:
        logger.warning('[Fonts] %s', e)
        return ''
    # Stable per-weight alias for O(1) reuse (content-addressed name would
    # need a scan to find by face/weight).
    os.makedirs(lib_dir, exist_ok=True)
    import hashlib
    digest = hashlib.sha256(data).hexdigest()[:20]
    stored = os.path.join(_store_dir(), font_id, f'{digest}{suffix}')
    try:
        if os.path.isfile(stored):
            if os.path.lexists(cached):
                os.unlink(cached)
            try:
                os.link(stored, cached)
            except OSError:
                import shutil
                shutil.copy2(stored, cached)
            logger.info('[Fonts] %s w%s ready: %s (%d bytes)',
                        font_id, src.weight, cached, len(data))
            return cached
    except OSError as e:
        logger.warning('[Fonts] alias failed for %s w%s: %s',
                       font_id, src.weight, e)
    return ''


def _store_dir() -> str:
    from lib.design_sys._store import store_root
    return os.path.join(store_root(), 'fonts')


def font_face_block(faces: list, *, rel_paths: dict) -> str:
    """The ``@font-face`` CSS block declaring every (face, weight) in use.

    ``rel_paths`` maps ``(font_id, weight)`` → scene-relative asset path, as
    returned by :func:`stage_font_into_scene`. Faces without a staged path
    are skipped — declaring a family whose file is absent is the silent-
    substitution trap the motion gates were built to catch.
    """
    blocks = []
    for face in faces:
        for src in face.sources:
            rel = rel_paths.get((face.id, src.weight))
            if not rel:
                continue
            blocks.append(
                f"@font-face {{ font-family: '{face.family}'; "
                f"src: url('{rel}') format('{src.fmt}'); "
                f"font-weight: {src.weight}; font-style: normal; "
                f"font-display: block; }}")
    return '\n'.join(blocks)


def stage_font_into_scene(scene_dir: str, font_id: str, weight: int) -> str:
    """Ensure + materialise one (face, weight) into a scene; return its
    scene-relative path ('assets/…') or '' (never raises)."""
    try:
        from lib.design_sys._store import materialise
        path = ensure_font(font_id, weight)
        if not path:
            return ''
        suffix = os.path.splitext(path)[1]
        rel, _tier = materialise(path, scene_dir,
                                 name=f'{font_id}-w{weight}{suffix}')
        return rel
    except Exception as e:
        logger.warning('[Fonts] staging %s w%s into %s failed: %s',
                       font_id, weight, scene_dir, e)
        return ''


def registry_summary() -> str:
    """One-line-per-face audit view (used by tests + diagnostics)."""
    rows = []
    for f in FONT_REGISTRY:
        weights = '/'.join(str(s.weight) for s in f.sources)
        rows.append(f'{f.id:20s} {f.family:18s} w[{weights}] '
                    f'{f.license_id}')
    return '\n'.join(rows)
