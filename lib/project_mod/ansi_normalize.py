# HOT_PATH
"""Terminal-output normalization for PTY-backed run_command streaming.

When a command runs under a pseudo-terminal (see ``run_command._run_command_pty``)
the child believes it is on a real terminal, so it emits its NATIVE live
output: progress bars that redraw in place via carriage-return (``\r``), ANSI
color codes, cursor moves, and line-erase sequences. Feeding that raw byte
stream straight into the output panel would (a) flood the buffer with thousands
of redraw frames of a single tqdm/pip bar, and (b) show raw escape sequences as
garbage.

``AnsiNormalizer`` turns that raw terminal stream into clean plain text with the
SAME visual result a terminal would show, via a simple LINE model:

  * committed lines  — text followed by ``\n`` (final, never rewritten again)
  * the current line — the line being drawn now; ``\r`` rewinds to column 0 so
    later chars OVERWRITE it. A tqdm bar that redraws ``\rEpoch 12%`` …
    ``\rEpoch 84%`` therefore stays ONE evolving current line, never stacking.

``feed(raw)`` returns ``(committed_delta, current_line)``:
  * ``committed_delta`` — newly finished lines (each with its ``\n``) produced
    by this chunk; append this to the output buffer.
  * ``current_line`` — the current in-progress line content (no newline);
    REPLACE the live tail with this.

This maps 1:1 onto the frontend protocol: ``_partialOutput += committed_delta``
(append) and ``_partialLine = current_line`` (replace). It is a pure, stateful
accumulator — no I/O, no timing — so it is unit-testable in isolation.

It is deliberately a LINE model, not a full screen model: vertical cursor moves
(``ESC[A`` / ``ESC[B``) are dropped. That is sufficient for the single-line
progress-bar case that motivates it while staying simple and safe.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)

# CSI sequence: ESC [ <params> <intermediates> <final-byte 0x40-0x7E>.
_CSI_RE = re.compile(r'\x1b\[([0-9;?]*)([ -/]*)([@-~])')
# OSC sequence: ESC ] ... (BEL | ESC \).
_OSC_RE = re.compile(r'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')
# Other 2-byte ESC sequences (ESC 7/8/M/=/> c etc.).
_ESC_SINGLE_RE = re.compile(r'\x1b[@-Z\\-_=>78Mc]')


class AnsiNormalizer:
    """Stateful normalizer: raw terminal bytes → clean text (line model)."""

    def __init__(self):
        self._cur = []        # chars of the line currently being drawn
        self._col = 0         # cursor column within self._cur
        self._pending = ''    # incomplete escape carried across chunk boundary

    # ── current-line primitives ────────────────────────────────────────
    def _write_char(self, ch):
        if self._col < len(self._cur):
            self._cur[self._col] = ch
        else:
            while len(self._cur) < self._col:
                self._cur.append(' ')
            self._cur.append(ch)
        self._col += 1

    def _erase_from_cursor(self):
        del self._cur[self._col:]

    # ── public API ─────────────────────────────────────────────────────
    def feed(self, text):
        """Consume raw text; return ``(committed_delta, current_line)``."""
        if not text:
            return '', ''.join(self._cur)
        if self._pending:
            text = self._pending + text
            self._pending = ''

        committed = []
        i, n = 0, len(text)
        while i < n:
            ch = text[i]
            if ch == '\x1b':
                consumed = self._handle_escape(text, i)
                if consumed == 0:            # incomplete escape at chunk end
                    self._pending = text[i:]
                    break
                i += consumed
                continue
            if ch == '\n':
                committed.append(''.join(self._cur))
                self._cur = []
                self._col = 0
                i += 1
                continue
            if ch == '\r':
                self._col = 0
                i += 1
                continue
            if ch == '\b':
                if self._col > 0:
                    self._col -= 1
                i += 1
                continue
            if ch == '\t':
                target = (self._col // 8 + 1) * 8
                while self._col < target:
                    self._write_char(' ')
                i += 1
                continue
            if ord(ch) < 0x20:               # bell, VT, FF, other C0 — drop
                i += 1
                continue
            self._write_char(ch)
            i += 1

        committed_delta = ''.join(line + '\n' for line in committed)
        return committed_delta, ''.join(self._cur)

    def flush(self):
        """Return any unterminated current line as a final committed delta."""
        self._pending = ''
        if self._cur:
            tail = ''.join(self._cur)
            self._cur = []
            self._col = 0
            return tail + '\n'
        return ''

    def _handle_escape(self, text, i):
        """Handle escape at text[i]; return chars consumed, or 0 if truncated."""
        m = _CSI_RE.match(text, i)
        if m:
            final, param = m.group(3), m.group(1)
            if final == 'K':                 # erase-in-line
                if param in ('', '0'):
                    self._erase_from_cursor()
                elif param == '2':
                    self._cur = []
                    self._col = 0
            # all other CSI finals (color 'm', cursor moves, …) are dropped
            return m.end() - i
        m = _OSC_RE.match(text, i)
        if m:
            return m.end() - i
        m = _ESC_SINGLE_RE.match(text, i)
        if m:
            return m.end() - i
        # Possibly a sequence truncated at the chunk boundary → stash (return 0).
        rest = text[i:]
        if (rest == '\x1b'
                or re.fullmatch(r'\x1b\[[0-9;?]*[ -/]*', rest)
                or re.fullmatch(r'\x1b\][^\x07\x1b]*', rest)
                or rest == '\x1b]'):
            return 0
        return 1                             # stray ESC — skip the byte
