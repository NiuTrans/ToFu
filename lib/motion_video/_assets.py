"""lib/motion_video/_assets.py — Scene asset store (content-addressed + linked).

Why this module exists (measured 2026-07-28, owner-directed). Every authored
scene could only ever be "a gradient plus text", and the cause was structural
rather than a missing tool: two rules combined to outlaw every non-text asset.

  * ``guide/COMPOSITION_CONTRACT.md`` forbids render-time network fetches for
    required assets — correct, that is what makes a render deterministic.
  * ``_scene_author.py`` deliberately withheld any filesystem-write tool, so a
    composition "can never reference a local asset path".

Net effect: the ONLY legal form an asset could take was a string inlined into
the HTML, i.e. inline SVG. Bitmaps, real screenshots and background video were
not merely unimplemented — they were structurally impossible. So the fix is a
channel, not a tool: assets land on disk FIRST, then a composition references
them by a scene-relative path, and the renderer sees only local files.

**Where assets live, and why it is not a free choice** (measured against the
real hyperframes CLI):

  * A path escaping the project root is REJECTED — ``../../assets/a.png``
    produced ``rc=1`` with ``Found 1 asset path(s) traversing above the project
    root with "../"`` plus ``<img> element references local file(s) not found
    in the project``. So a global library CANNOT be referenced directly.
  * A hardlink, a symlink, and a scene subdirectory all PASS (``rc=0``).

Hence: one content-addressed library holds the bytes ONCE, and each scene gets
its own link into that library. The library de-duplicates across jobs; the
link satisfies the renderer's root constraint without copying bytes.

**Three-tier materialisation** (hardlink → symlink → copy). Each tier is
attempted in turn and the tier actually used is reported.

The fallback is NOT hypothetical — it is load-bearing on this very host, and
for a reason worth writing down. The library and the job dirs sit on the SAME
device (both under the data root), so a hardlink "should" work; measured, it
does not: dolphinfs answers ``os.link`` with ``EPERM`` (*Operation not
permitted*) rather than ``EXDEV``. A same-device check would therefore have
concluded "hardlink is fine" and every materialisation would have failed. On
top of that ``/tmp`` really is a different device here, so a workdir relocated
there yields the textbook ``EXDEV`` too. Hence: never predict which tier will
work — try them in order of preference and report the winner.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil

from lib.log import get_logger

logger = get_logger(__name__)

__all__ = ['asset_root', 'store_bytes', 'store_file', 'materialise',
           'scene_asset_dir', 'verify_asset_refs', 'extract_local_refs',
           'SCENE_ASSET_SUBDIR', 'AssetError']

#: Subdirectory INSIDE a scene project that holds its linked assets. A plain
#: subdirectory (not the project root) so the renderer's "multiple root-level
#: HTML files" scan is never tripped by an asset, and so a composition's
#: references are short and obviously scene-local.
SCENE_ASSET_SUBDIR = 'assets'

#: Extensions we are willing to materialise into a scene. Deliberately a
#: WHITELIST: the store is reachable from an LLM-driven tool, so an unknown
#: extension is refused rather than written.
ALLOWED_EXTENSIONS = frozenset({
    '.png', '.jpg', '.jpeg', '.webp', '.avif', '.gif', '.svg',   # images
    '.mp4', '.webm',                                             # video
    '.otf', '.ttf', '.woff', '.woff2',                           # fonts
})


class AssetError(Exception):
    """A refused asset operation (bad extension, missing source, escape)."""


def asset_root() -> str:
    """The shared, content-addressed asset library.

    One library for every job: the same generated background or downloaded
    logo is stored once and linked into as many scenes as reference it.
    """
    from lib.motion_video._env import motion_root
    path = os.path.join(motion_root(), 'assets')
    os.makedirs(path, exist_ok=True)
    return path


def scene_asset_dir(scene_dir: str) -> str:
    path = os.path.join(scene_dir, SCENE_ASSET_SUBDIR)
    os.makedirs(path, exist_ok=True)
    return path


def _ext_of(name: str) -> str:
    """Validate + return the lowercase extension of a filename OR a suffix.

    Accepts all three shapes callers really produce: a filename
    (``logo.png``), a dotted suffix (``.png``, what ``os.path.splitext``
    returns) and a bare token (``png``, what a MIME map or an API field
    yields). ``os.path.splitext('.png')`` returns ``('.png', '')`` — an EMPTY
    extension — so a suffix must be recognised before splitting, or every
    ``store_bytes`` call is refused.
    """
    stripped = (name or '').strip().lower()
    if not stripped:
        raise AssetError('refusing an asset with no extension')
    if '.' not in stripped:
        ext = '.' + stripped                       # bare token: 'png'
    elif stripped.startswith('.') and '.' not in stripped[1:]:
        ext = stripped                             # dotted suffix: '.png'
    else:
        ext = os.path.splitext(stripped)[1]        # filename: 'logo.png'
    if ext not in ALLOWED_EXTENSIONS:
        raise AssetError(
            f'refusing asset with extension {ext!r} — allowed: '
            + ', '.join(sorted(ALLOWED_EXTENSIONS)))
    return ext


def store_bytes(data: bytes, *, suffix: str) -> str:
    """Put ``data`` in the library under its content hash; return the path.

    Content addressing is what makes the library self-de-duplicating: the same
    bytes written twice occupy one file, so re-generating an identical
    background across scenes (or across jobs) costs nothing.
    """
    if not data:
        raise AssetError('refusing to store an empty asset')
    ext = _ext_of(suffix)
    digest = hashlib.sha256(data).hexdigest()[:20]
    dest = os.path.join(asset_root(), f'{digest}{ext}')
    if os.path.isfile(dest) and os.path.getsize(dest) == len(data):
        logger.info('[Assets] reuse %s (%d bytes, already in the library)',
                    os.path.basename(dest), len(data))
        return dest
    tmp = dest + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, dest)              # atomic: a torn asset is unusable
    logger.info('[Assets] stored %s (%d bytes)', os.path.basename(dest),
                len(data))
    return dest


def store_file(path: str) -> str:
    """Copy an existing file into the library by content hash."""
    if not os.path.isfile(path):
        raise AssetError(f'no such asset file: {path}')
    with open(path, 'rb') as f:
        data = f.read()
    return store_bytes(data, suffix=os.path.splitext(path)[1])


def materialise(library_path: str, scene_dir: str, *,
                name: str = '') -> tuple[str, str]:
    """Link a library asset into ``scene_dir``; return ``(rel_path, tier)``.

    ``rel_path`` is what a composition must use (e.g. ``assets/ab12cd.png``) —
    scene-relative, never escaping the project root, because the renderer
    rejects ``../`` outright.

    ``tier`` is the mechanism that actually worked: ``'hardlink'`` (no bytes
    copied, survives library pruning), ``'symlink'`` (no bytes copied, breaks
    if the library entry is removed) or ``'copy'`` (independent bytes). The
    tiers are tried in that order because a hardlink is strictly best when
    available, and NEVER predicted: measured on this host, dolphinfs refuses
    ``os.link`` with ``EPERM`` even for two paths on the SAME device, so a
    same-device test would wrongly conclude hardlinks work. A workdir under
    ``/tmp`` additionally fails with ``EXDEV``. Both cases must degrade
    silently to the next tier.
    """
    if not os.path.isfile(library_path):
        raise AssetError(f'library asset missing: {library_path}')
    dest_dir = scene_asset_dir(scene_dir)
    base = name or os.path.basename(library_path)
    _ext_of(base)
    dest = os.path.join(dest_dir, base)
    rel = os.path.join(SCENE_ASSET_SUBDIR, base)

    if os.path.lexists(dest):
        # Already materialised (resume path) — trust it only if it resolves.
        if os.path.isfile(dest):
            return rel, 'existing'
        os.unlink(dest)                 # a dangling symlink from a past run

    for tier, fn in (('hardlink', os.link),
                     ('symlink', os.symlink),
                     ('copy', shutil.copy2)):
        try:
            fn(library_path, dest)
        except OSError as e:
            logger.info('[Assets] %s failed for %s (%s) — trying the next '
                        'tier', tier, base, e.__class__.__name__)
            continue
        except Exception as e:
            logger.warning('[Assets] %s raised for %s: %s', tier, base, e)
            continue
        if not os.path.isfile(dest):
            # A symlink that resolves nowhere is worse than no asset: the
            # renderer would report a missing file instead of a broken link.
            try:
                os.unlink(dest)
            except OSError as _e:
                logger.debug('[Assets] cleanup of unusable %s: %s', dest, _e)
            continue
        logger.info('[Assets] materialised %s into %s via %s', base,
                    scene_dir, tier)
        return rel, tier
    raise AssetError(f'could not materialise {library_path} into {dest_dir} '
                     '(hardlink, symlink and copy all failed)')


#: Local references a composition may carry. Data URIs and http(s) are handled
#: separately (a CDN <script>/<link> at page load is allowed by the contract;
#: a data: URI carries its own bytes).
_REF_RE = re.compile(
    r'''(?:src|href)\s*=\s*["']([^"']+)["']'''
    r'''|url\(\s*["']?([^"')]+)["']?\s*\)''',
    re.IGNORECASE)


def extract_local_refs(html: str) -> list[str]:
    """Every LOCAL file reference in ``html`` (skips data:/http(s):/#anchors)."""
    out: list[str] = []
    for m in _REF_RE.finditer(html or ''):
        ref = (m.group(1) or m.group(2) or '').strip()
        if not ref:
            continue
        low = ref.lower()
        if low.startswith(('data:', 'http://', 'https://', '//', '#',
                           'mailto:', 'blob:')):
            continue
        out.append(ref)
    return out


def verify_asset_refs(html: str, scene_dir: str) -> list[str]:
    """Findings for every local asset reference that will not render.

    Runs BEFORE Chrome. That ordering is the point: the renderer reports a
    missing asset only after a browser boot (and a render is ~3.5x realtime),
    so an asset defect found here costs nothing while the same defect found
    downstream costs a wasted render — or ships a frame with a broken image.

    Three refusals, all of which occur in real runs, so catching only the
    first would be no protection at all:

      1. ``../`` escaping the project root — measured: the CLI rejects it with
         "asset path(s) traversing above the project root";
      2. an ABSOLUTE path — it points outside the project the renderer serves,
         so it cannot be part of a portable, deterministic composition;
      3. a reference whose file simply is NOT THERE — the most common one once
         a model starts inventing filenames.
    """
    findings: list[str] = []
    # Containment is judged on the LEXICAL path, not the resolved one. This is
    # load-bearing: a correctly materialised asset is usually a SYMLINK into
    # the shared library, so `realpath` points OUTSIDE the scene by design —
    # resolving first therefore flags every legitimate asset as an escape
    # (measured: it rejected the very assets that render correctly). What the
    # renderer actually forbids is a reference that TRAVERSES above the
    # project root, which is a property of the path as written.
    for ref in extract_local_refs(html):
        clean = ref.split('#', 1)[0].split('?', 1)[0]
        if not clean:
            continue
        if os.path.isabs(clean):
            findings.append(
                f'asset reference {ref!r} is an ABSOLUTE path — a composition '
                f'may only reference files inside its own project directory '
                f'(use {SCENE_ASSET_SUBDIR}/<name> after storing the asset)')
            continue
        # normpath collapses 'a/../b'; a leading '..' survives only for a
        # reference that really does climb out of the project.
        normalised = os.path.normpath(clean)
        if normalised == os.pardir or normalised.startswith(os.pardir + os.sep):
            findings.append(
                f'asset reference {ref!r} escapes the project root — the '
                f'renderer rejects paths traversing above it; store the asset '
                f'and reference it as {SCENE_ASSET_SUBDIR}/<name>')
            continue
        # Existence follows the link on purpose: a DANGLING symlink must be
        # reported, since the renderer would draw nothing for it.
        if not os.path.isfile(os.path.join(scene_dir, normalised)):
            findings.append(
                f'asset reference {ref!r} does not exist in the scene '
                f'directory — store the file first, then reference the path '
                f'the store returned')
    if findings:
        logger.warning('[Assets] %d unusable asset reference(s) in %s',
                       len(findings), scene_dir)
    return findings
