"""lib/qr.py — QR codes as a first-class display payload.

Two directions, one module:

**Generation** (``qr_png_data_uri`` / ``save_qr_png``) — turn a payload (a
scan-to-login URL, a pairing token) into a PNG a browser can render. A QR PNG
is ~1 KB, so it can ride a data URI without tripping the binary-blob-in-text
defenses (tests/test_binary_blob_text_stream_guard.py). The SVG form is ~13x
larger for identical content, so PNG is the only form emitted.

**Detection** (``terminal_qr_images``) — recover a REAL bitmap from the
ASCII/Unicode block art that CLI tools print when they want you to scan
something (``docker login``, ``gh auth``, wrangler, any ``qrcode.print_ascii``
caller). This exists because terminal QR art is not merely ugly in the chat
transcript, it is **structurally unscannable**: the terminal-output pane
(``.ptool-cmd-output`` in static/styles.css) sets ``white-space: pre-wrap``
with ``word-break: break-all``, which re-wraps the module rows at arbitrary
columns and destroys the 2-D grid. No amount of restyling fixes a QR that the
user must point a phone at — it has to become an image.

Polarity is NOT assumed. Terminals disagree about whether a block glyph means
a dark module (``qrcode.print_ascii``) or a light one (``print_ascii(tty=True)``
and most Go/Node QR CLIs, which print reverse-video). Guessing wrong yields a
photographic negative, which no scanner reads. So every plausible reading is
built and then **validated against the QR standard's own finder patterns** —
the three 7x7 bullseyes at the corners — and only a reading that actually
validates is emitted. That makes the decoder self-correcting rather than
tuned to whichever CLI was tested first.

Dependencies: detection needs only Pillow (declared). Generation additionally
needs ``qrcode``; it is imported lazily so a missing package degrades that one
call instead of breaking import of this module.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import re

from lib.log import get_logger

logger = get_logger(__name__)

# ── Glyph vocabulary ──────────────────────────────────────────────────
# Upper/lower half blocks let one text line carry TWO module rows, which is
# what ``qrcode.print_ascii`` emits (cp437 220/223/219 + 255 as the blank).
_UPPER_HALF = '\u2580'   # ▀ top half set
_LOWER_HALF = '\u2584'   # ▄ bottom half set
_FULL_BLOCK = '\u2588'   # █ both halves set
_NBSP = '\xa0'           # print_ascii's blank (cp437 255) — NOT a plain space

# Glyphs that stand for a solid cell in the non-half-block styles.
_SOLID = frozenset({_FULL_BLOCK, '#', '@', '\u2593', '\u2592', '\u25a0'})
# Glyphs that stand for an empty cell.
_BLANK = frozenset({' ', _NBSP, '.', '-', '_'})

_ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')

# A QR symbol is 21..177 modules square (version 1..40, step 4).
_MIN_MODULES = 21
_MAX_MODULES = 177

# The 7x7 finder pattern stamped at three corners of every QR symbol.
_FINDER = (
    (1, 1, 1, 1, 1, 1, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 1, 1, 1, 0, 1),
    (1, 0, 0, 0, 0, 0, 1),
    (1, 1, 1, 1, 1, 1, 1),
)

# Cheap pre-check: skip the whole scan unless the text carries art glyphs.
# MUST cover every glyph a builder can read as a solid cell, or a style is
# silently unreachable: gating on block glyphs alone made ``_grid_cells``'
# ``#``/``@`` support dead code (the candidate scan rejected the block before
# any builder ran).
_ART_GLYPHS = tuple(sorted(_SOLID | {_UPPER_HALF, _LOWER_HALF}))

# Bounds so a pathological payload can't turn detection into a CPU sink.
_MAX_SCAN_CHARS = 512 * 1024
_MAX_QRS = 4
_MIN_BLOCK_LINES = 8


# ═══════════════════════════════════════════════════════════════════════
#  Generation
# ═══════════════════════════════════════════════════════════════════════

def qr_png_data_uri(payload: str, *, scale: int = 6, border: int = 3) -> str:
    """Encode *payload* as a QR PNG ``data:`` URI.

    Args:
        payload: The text/URL to encode.
        scale: Pixels per module.
        border: Quiet-zone width in modules (the spec requires >= 4; 3 is
            accepted here because the renderer adds page padding around it).

    Returns:
        A ``data:image/png;base64,...`` string, or ``''`` when the ``qrcode``
        package is unavailable or encoding fails (callers must treat an empty
        string as "no QR available" rather than assuming success).
    """
    if not payload:
        logger.warning('[QR] qr_png_data_uri called with empty payload')
        return ''
    try:
        import qrcode
    except ImportError as e:
        logger.error('[QR] qrcode package unavailable — cannot generate: %s', e)
        return ''
    try:
        qr = qrcode.QRCode(box_size=scale, border=border)
        qr.add_data(payload)
        qr.make()
        return matrix_to_png_data_uri(
            [[1 if v else 0 for v in row] for row in qr.get_matrix()],
            scale=scale, quiet=0)
    except Exception as e:
        logger.error('[QR] generation failed for %d-char payload: %s',
                     len(payload), e, exc_info=True)
        return ''


def save_qr_png(payload: str, dest_dir: str, *,
                filename: str = '', scale: int = 6, border: int = 3) -> str:
    """Write a QR PNG into *dest_dir* and return its basename.

    Intended for the scan-to-login flow: drop the PNG into the served uploads
    directory and reference it as ``/api/images/<basename>`` from an
    ``ask_human`` question, so the blocking prompt shows a real scannable code
    without a multi-KB base64 blob riding the question text (which the
    auto-translate pass would otherwise feed to an LLM verbatim).

    Returns the basename, or ``''`` on failure.
    """
    import os
    import uuid

    uri = qr_png_data_uri(payload, scale=scale, border=border)
    if not uri:
        return ''
    name = filename or f'qr_{uuid.uuid4().hex}.png'
    path = os.path.join(dest_dir, os.path.basename(name))
    try:
        raw = base64.b64decode(uri.split(',', 1)[1])
        with open(path, 'wb') as fh:
            fh.write(raw)
    except Exception as e:
        logger.error('[QR] failed writing PNG to %s: %s', path, e, exc_info=True)
        return ''
    logger.info('[QR] wrote %s (%d bytes)', path, len(raw))
    return os.path.basename(path)


def qr_login_question(payload: str, *, prompt: str = '',
                      alt: str = 'QR code') -> str:
    """Build an ``ask_human`` question body that DISPLAYS a scannable QR.

    The QR is referenced as a served ``/api/images/<name>`` URL, never inlined
    as base64, for two evidence-backed reasons:

    1. When ``conv.autoTranslate`` is on, the frontend sends the WHOLE question
       string to the translation API and renders the result
       (``_autoTranslateHumanGuidance`` in static/js/ui/stream_lifecycle.js).
       A 1.5k-char base64 blob would be shipped to an LLM as prose and come
       back mangled — an unscannable code. A short sentence plus a URL
       translates cleanly.
    2. ``renderMarkdown`` rewrites root-anchored ``/api/...`` image URLs with
       ``BASE_PATH`` (static/js/core/markdown.js), so the code still loads
       behind a reverse proxy / cloud-IDE prefix. A hand-written ``<img>`` gets
       no such treatment.

    The question renders through the full markdown pipeline
    (``tool_rounds.js`` → ``renderMarkdown``), and DOMPurify keeps ``<img>``
    with both ``data:`` and path URLs, so the image is displayed inline while
    the agent loop BLOCKS on the human's answer.

    Returns ``''`` when the QR could not be produced, so the caller can fall
    back to showing the raw URL rather than asking the user to scan nothing.
    """
    from lib.runtime_paths import uploads_root

    dest = os.path.join(uploads_root(), 'images')
    try:
        os.makedirs(dest, exist_ok=True)
    except OSError as e:
        logger.error('[QR] cannot create image dir %s: %s', dest, e)
        return ''
    name = save_qr_png(payload, dest)
    if not name:
        return ''
    head = prompt or 'Scan this QR code to continue, then confirm below.'
    return f'{head}\n\n![{alt}](/api/images/{name})\n'


def matrix_to_png_data_uri(matrix, *, scale: int = 6, quiet: int = 4) -> str:
    """Render a module matrix (1 = dark) to a PNG ``data:`` URI.

    A nearest-neighbour block scale-up — never interpolated, because a blurred
    module edge is what makes a re-rendered QR undecodable.
    """
    if not matrix or not matrix[0]:
        return ''
    try:
        from PIL import Image
    except ImportError as e:
        logger.error('[QR] Pillow unavailable — cannot render matrix: %s', e)
        return ''
    try:
        h, w = len(matrix), len(matrix[0])
        img = Image.new('L', (w + 2 * quiet, h + 2 * quiet), 255)
        px = img.load()
        for y, row in enumerate(matrix):
            for x, v in enumerate(row):
                if v:
                    px[x + quiet, y + quiet] = 0
        if scale > 1:
            img = img.resize((img.width * scale, img.height * scale),
                             Image.NEAREST)
        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        return 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception as e:
        logger.error('[QR] matrix render failed: %s', e, exc_info=True)
        return ''


# ═══════════════════════════════════════════════════════════════════════
#  Validation — the QR standard is the arbiter, not our guess
# ═══════════════════════════════════════════════════════════════════════

def _finder_at(m, top: int, left: int) -> bool:
    for dy in range(7):
        row = m[top + dy]
        for dx in range(7):
            if row[left + dx] != _FINDER[dy][dx]:
                return False
    return True


def is_valid_qr_matrix(m) -> bool:
    """True when *m* is dimensionally a QR symbol AND carries all three
    corner finder patterns.

    This is the whole reason polarity never has to be assumed: a negated or
    mis-sheared reading fails here, so a wrong hypothesis is discarded rather
    than shipped as an unscannable image.
    """
    if not m or not m[0]:
        return False
    h, w = len(m), len(m[0])
    if h != w or h < _MIN_MODULES or h > _MAX_MODULES or (h - 17) % 4:
        return False
    if any(len(row) != w for row in m):
        return False
    return (_finder_at(m, 0, 0)
            and _finder_at(m, 0, w - 7)
            and _finder_at(m, h - 7, 0))


# ═══════════════════════════════════════════════════════════════════════
#  Detection
# ═══════════════════════════════════════════════════════════════════════

def _trim(m):
    """Strip the quiet zone, whichever colour it is.

    Trimming only light borders is not enough: reverse-video art (``tty=True``,
    ``invert=True``, and most Go/Node QR CLIs) prints the quiet zone as SOLID,
    so after inversion the margin is DARK. Stripping only light rows then eats
    into the symbol from one side and shifts the grid — the finder patterns
    land off-corner and a genuinely-good QR is rejected. So peel a uniform
    border of EITHER colour, and let the finder-pattern check arbitrate.
    """
    if not m or not m[0]:
        return []
    top, bot = 0, len(m) - 1
    left, right = 0, len(m[0]) - 1

    def _uniform_row(i, lo, hi):
        row = m[i]
        return row[lo] if all(row[j] == row[lo] for j in range(lo, hi + 1)) else None

    def _uniform_col(j, lo, hi):
        v = m[lo][j]
        return v if all(m[i][j] == v for i in range(lo, hi + 1)) else None

    changed = True
    while changed and top < bot and left < right:
        changed = False
        if _uniform_row(top, left, right) is not None:
            top += 1
            changed = True
        if bot > top and _uniform_row(bot, left, right) is not None:
            bot -= 1
            changed = True
        if _uniform_col(left, top, bot) is not None:
            left += 1
            changed = True
        if right > left and _uniform_col(right, top, bot) is not None:
            right -= 1
            changed = True
    # Peeling stops exactly AT the symbol edge and must not step back out: a
    # QR's outermost row spans BOTH corner finders plus the light separator
    # gap between them, so it is never uniform. (An earlier version widened by
    # one here on the theory that a finder's edge line is uniform — it is not,
    # and the over-expansion turned a valid 29-module read into a rejected
    # 31-module one, silently breaking every half-block style.)
    return [row[left:right + 1] for row in m[top:bot + 1]]


def _collapse_columns(m):
    """Undo integer horizontal scaling (``██`` per module doubles the width).

    Terminal cells are ~2x taller than wide, so QR CLIs commonly print each
    module as two characters to keep the symbol square on screen.
    """
    if not m or not m[0]:
        return m
    h, w = len(m), len(m[0])
    if h == 0 or w % h:
        return m
    k = w // h
    if k < 2:
        return m
    return [row[::k] for row in m]


def _grid_half_block(lines, solid_is_dark: bool):
    """Decode half-block art: each text line carries two module rows."""
    out = []
    for line in lines:
        top, bot = [], []
        for ch in line:
            if ch == _FULL_BLOCK:
                t = b = 1
            elif ch == _UPPER_HALF:
                t, b = 1, 0
            elif ch == _LOWER_HALF:
                t, b = 0, 1
            else:
                t = b = 0
            if not solid_is_dark:
                t, b = 1 - t, 1 - b
            top.append(t)
            bot.append(b)
        out.append(top)
        out.append(bot)
    return out


def _grid_cells(lines, solid_is_dark: bool):
    """Decode one-line-per-row art (``██`` / ``##`` / spaces)."""
    out = []
    for line in lines:
        row = []
        for ch in line:
            if ch in _SOLID:
                v = 1
            elif ch in _BLANK:
                v = 0
            else:
                v = 0
            row.append(v if solid_is_dark else 1 - v)
        out.append(row)
    return out


def _candidate_blocks(text: str):
    """Yield each run of consecutive art-bearing lines, padded to a rectangle.

    Deliberately does NOT crop columns to where art glyphs sit. In
    ``qrcode.print_ascii`` a DARK module can be the blank glyph (cp437 255 /
    NBSP), so an art-glyph column window slices real modules off the edge (a
    29-module symbol came through as 25 columns and was rejected). Column
    alignment is instead left to :func:`_trim`, which peels a uniform border
    of either colour — that also disposes of log prefixes and indentation for
    free, since non-art characters read as a uniform margin.
    """
    lines = text.split('\n')
    run: list[int] = []
    for idx in range(len(lines) + 1):
        if idx < len(lines) and any(g in lines[idx] for g in _ART_GLYPHS):
            run.append(idx)
            continue
        if len(run) >= _MIN_BLOCK_LINES:
            seg = lines[run[0]:run[-1] + 1]
            width = max(len(ln) for ln in seg)
            yield [ln.ljust(width) for ln in seg]
        run = []


def detect_terminal_qr_matrices(text: str):
    """Find QR symbols rendered as terminal art in *text*.

    Returns a list of module matrices (1 = dark). Only readings that satisfy
    :func:`is_valid_qr_matrix` are returned, so a false positive requires
    accidentally reproducing three 7x7 finder patterns at exact QR corners.
    """
    if not text or len(text) > _MAX_SCAN_CHARS:
        return []
    if not any(g in text for g in _ART_GLYPHS):
        return []
    clean = _ANSI_RE.sub('', text)
    found = []
    for lines in _candidate_blocks(clean):
        for builder in (_grid_half_block, _grid_cells):
            for solid_is_dark in (True, False):
                grid = builder(lines, solid_is_dark)
                cand = _collapse_columns(_trim(grid))
                if is_valid_qr_matrix(cand):
                    found.append(cand)
                    break
            else:
                continue
            break
        if len(found) >= _MAX_QRS:
            break
    if found:
        logger.info('[QR] recovered %d scannable QR symbol(s) from terminal art',
                    len(found))
    return found


def terminal_qr_images(text: str, *, scale: int = 6):
    """Terminal art → inline-renderable image descriptors.

    Returns a list shaped like the ``imageDataUris`` descriptors the chat
    timeline already renders (``uri`` / ``format`` / ``filename``), so a
    recovered QR rides the existing image-render path instead of needing a
    bespoke transport.
    """
    out = []
    for i, m in enumerate(detect_terminal_qr_matrices(text)):
        uri = matrix_to_png_data_uri(m, scale=scale)
        if not uri:
            continue
        out.append({
            'uri': uri,
            'format': 'png',
            'filename': f'qr-{i + 1}.png' if i else 'qr.png',
            'modules': len(m),
            'source': 'terminal',
        })
    return out


def qr_signature(descriptor) -> str:
    """Stable identity for a recovered QR, used to avoid re-emitting one code.

    Keyed on the rendered image bytes, so two scans of the same symbol collapse
    to one entry while a genuinely different code (a refreshed login token)
    registers as new. The ``uri`` already encodes the full module matrix, so
    hashing it is equivalent to hashing the matrix without re-serialising it.
    """
    if not isinstance(descriptor, dict):
        return ''
    uri = descriptor.get('uri') or ''
    if not uri:
        return ''
    return hashlib.sha256(uri.encode('ascii', errors='ignore')).hexdigest()[:16]


class LiveQrScanner:
    """Incrementally scan a GROWING terminal buffer for QR art.

    Why this exists: the whole point of a scan-to-login QR is that the command
    is still RUNNING — ``gh auth login`` / device-code flows print the code and
    then block waiting for the scan. A QR that only materialises when the
    process exits arrives after the authorization window has closed (or after
    the command timed out for lack of a scan), which is the same as not having
    it. So detection has to happen on the live ``tool_progress`` path, not only
    at finalize.

    Two properties the naive "re-scan everything on every chunk" version lacks:

    * **Idempotent** — a chunk arrives every ~200 ms, and the art sits in the
      buffer for the entire wait. Without dedup the same code would be
      re-attached and re-emitted dozens of times, flooding the SSE channel with
      multi-KB frames. Signatures are remembered, so each distinct code is
      emitted exactly once.
    * **Bounded** — a full re-scan of a large buffer per chunk is O(n) work per
      chunk, i.e. O(n²) over the command. Scanning is therefore gated on a
      cheap art-glyph check, skipped until the buffer holds enough lines to
      contain a symbol, throttled to a minimum growth delta between attempts,
      and capped at a tail window (a QR is drawn as one contiguous block, so
      the recent tail is where an unseen one can be).

    Not thread-safe by itself; the caller (the run_command progress callback)
    already holds its flush lock.
    """

    #: Only rescan once the buffer has grown by at least this much since the
    #: last attempt — a QR block is thousands of chars, so sub-KB growth cannot
    #: complete a previously-absent symbol.
    _MIN_GROWTH = 512
    #: Tail window scanned on each attempt. Comfortably larger than the biggest
    #: terminal QR (a 177-module symbol printed 2 chars wide is ~63 KB).
    _TAIL_CHARS = 96 * 1024

    def __init__(self):
        self._seen: set[str] = set()
        self._last_len = 0

    def scan(self, buffer: str):
        """Return descriptors for codes in *buffer* not yet reported.

        Callers pass the FULL accumulated buffer each time; the scanner does
        the windowing. Returns ``[]`` when there is nothing new — which is the
        overwhelmingly common case and must stay cheap.
        """
        if not buffer:
            return []
        n = len(buffer)
        if n - self._last_len < self._MIN_GROWTH and self._last_len:
            return []
        window = buffer[-self._TAIL_CHARS:] if n > self._TAIL_CHARS else buffer
        if not any(g in window for g in _ART_GLYPHS):
            self._last_len = n
            return []
        if window.count('\n') < _MIN_BLOCK_LINES:
            self._last_len = n
            return []
        self._last_len = n
        fresh = []
        try:
            for d in terminal_qr_images(window):
                sig = qr_signature(d)
                if not sig or sig in self._seen:
                    continue
                self._seen.add(sig)
                fresh.append(d)
        except Exception as e:
            logger.warning('[QR] live scan failed (non-fatal): %s', e)
            return []
        if fresh:
            logger.info('[QR] live scan surfaced %d new QR code(s) mid-command',
                        len(fresh))
        return fresh


__all__ = [
    'qr_png_data_uri',
    'save_qr_png',
    'qr_login_question',
    'matrix_to_png_data_uri',
    'is_valid_qr_matrix',
    'detect_terminal_qr_matrices',
    'terminal_qr_images',
    'qr_signature',
    'LiveQrScanner',
]
