"""Guards for QR display: generation, terminal-art recovery, and the two
seams that put a scannable code in front of the user.

Why this suite exists
---------------------
A QR code is the one payload whose ONLY acceptance criterion is "a phone can
read it". Asserting that pixels exist, or that some bytes were attached, would
pass while shipping an unscannable code. So the central tests **decode the
delivered image back to the original payload** with an independent decoder
(OpenCV) and compare strings. Tests that cannot decode (no cv2 installed)
fall back to the QR standard's own structural invariant — the three corner
finder patterns — never to "looks about right".

The root causes pinned here, each of which shipped a silently-broken code:

* Terminal QR art is **structurally** unscannable in the chat transcript, not
  merely ugly: ``.ptool-cmd-output`` is ``white-space: pre-wrap`` +
  ``word-break: break-all``, which re-wraps module rows. Hence the bitmap.
* Polarity differs per tool (``qrcode.print_ascii`` draws DARK modules as
  block glyphs; reverse-video CLIs draw LIGHT ones). A wrong guess yields a
  photographic negative that no scanner reads, so polarity must be *derived*.
* In ``print_ascii`` a dark module can BE the blank glyph (cp437 255 / NBSP),
  so cropping the art to block-glyph columns silently truncates the symbol.
* An inverted symbol has a DARK quiet zone, so trimming only light borders
  mis-aligns the grid.

Run: pytest tests/test_qr_display.py -v
"""
from __future__ import annotations

import base64
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

pytestmark = pytest.mark.unit

PAYLOAD = 'https://passport.example.com/qrlogin?token=Xy9_ABC-123'


# ── helpers ───────────────────────────────────────────────────────────

def _qr_matrix(payload=PAYLOAD, border=2):
    qrcode = pytest.importorskip('qrcode')
    qr = qrcode.QRCode(border=border)
    qr.add_data(payload)
    qr.make()
    return qr.get_matrix()


def _symbol_modules(payload=PAYLOAD, border=2):
    """The symbol's true module count.

    NOT ``len(get_matrix())`` — that includes the quiet zone on both sides
    (border=4 around a version-1 symbol gives 21 + 2*4 = 29). Recovery strips
    the quiet zone, so the bare ``modules_count`` is the right comparand;
    conflating the two made an earlier assertion here compare 21 against 29.
    """
    qrcode = pytest.importorskip('qrcode')
    qr = qrcode.QRCode(border=border)
    qr.add_data(payload)
    qr.make()
    return qr.modules_count


def _print_ascii(payload=PAYLOAD, border=2, invert=False):
    """The real ``qrcode.print_ascii`` output — half-block, NBSP-padded."""
    qrcode = pytest.importorskip('qrcode')
    qr = qrcode.QRCode(border=border)
    qr.add_data(payload)
    qr.make()
    buf = io.StringIO()
    qr.print_ascii(out=buf, invert=invert)
    return buf.getvalue()


def _cells(matrix, on='██', off='  '):
    return '\n'.join(''.join(on if v else off for v in row) for row in matrix)


def _decode(data_uri):
    """Decode a QR data URI with OpenCV. Returns the payload, or None when no
    decoder is installed (callers then fall back to structural checks)."""
    cv2 = pytest.importorskip('cv2', reason='OpenCV needed to truly decode')
    import numpy as np
    from PIL import Image
    raw = base64.b64decode(data_uri.split(',', 1)[1])
    img = np.array(Image.open(io.BytesIO(raw)).convert('L'))
    text, *_ = cv2.QRCodeDetector().detectAndDecode(img)
    return text


# ═══════════════════════════════════════════════════════════════════════
#  Generation — a produced code must be readable, not merely produced
# ═══════════════════════════════════════════════════════════════════════

class TestGeneration:
    def test_generated_qr_decodes_back_to_payload(self):
        from lib.qr import qr_png_data_uri
        uri = qr_png_data_uri(PAYLOAD)
        assert uri.startswith('data:image/png;base64,')
        assert _decode(uri) == PAYLOAD, 'generated QR does not scan'

    def test_png_is_small_enough_to_inline(self):
        """The size claim that justifies inlining at all: a QR PNG is ~1 KB,
        so it does not belong to the multi-MB blob class the binary-blob
        guards defend against (tests/test_binary_blob_text_stream_guard.py)."""
        from lib.qr import qr_png_data_uri
        assert len(qr_png_data_uri(PAYLOAD)) < 8000

    def test_empty_payload_returns_empty_not_a_blank_code(self):
        """A blank image would look like a working QR and scan as nothing."""
        from lib.qr import qr_png_data_uri
        assert qr_png_data_uri('') == ''

    def test_save_writes_a_scannable_file(self, tmp_path):
        from lib.qr import save_qr_png
        name = save_qr_png(PAYLOAD, str(tmp_path))
        assert name and (tmp_path / name).exists()
        raw = (tmp_path / name).read_bytes()
        assert raw[:8] == b'\x89PNG\r\n\x1a\n'
        uri = 'data:image/png;base64,' + base64.b64encode(raw).decode()
        assert _decode(uri) == PAYLOAD

    def test_save_confines_filename_to_dest_dir(self, tmp_path):
        """A traversing filename must not escape the served image directory."""
        from lib.qr import save_qr_png
        name = save_qr_png(PAYLOAD, str(tmp_path), filename='../evil.png')
        assert name == 'evil.png'
        assert (tmp_path / 'evil.png').exists()
        assert not (tmp_path.parent / 'evil.png').exists()


# ═══════════════════════════════════════════════════════════════════════
#  Detection — every terminal style must survive to a scannable image
# ═══════════════════════════════════════════════════════════════════════

class TestTerminalRecovery:
    """Each case is a real encoding a CLI actually emits. The assertion is
    always a DECODE, because 'found a QR' and 'produced a scannable QR' are
    different claims and only the second one matters to the user."""

    def _styles(self):
        m = _qr_matrix()
        return {
            # qrcode.print_ascii: half-block, block glyph = DARK, NBSP blank
            'print_ascii': _print_ascii(),
            # inverted: block glyph = LIGHT and the quiet zone is DARK
            'print_ascii_invert': _print_ascii(invert=True),
            'full_block': _cells(m),
            'single_width_block': _cells(m, '█', ' '),
            'hash': _cells(m, '##', '  '),
            'at_sign': _cells(m, '@@', '  '),
            'reverse_video': _cells(m, '  ', '██'),
            'ansi_colored': '\n'.join(
                '\x1b[40m\x1b[37m' + ''.join('██' if v else '  ' for v in r) + '\x1b[0m'
                for r in m),
            'log_prefixed': '\n'.join(
                '[info] ' + ''.join('██' if v else '  ' for v in r) for r in m),
            'crlf': _cells(m).replace('\n', '\r\n'),
            'embedded_in_prose': 'Scan to log in:\n' + _cells(m) + '\nWaiting...\n',
        }

    @pytest.mark.parametrize('style', [
        'print_ascii', 'print_ascii_invert', 'full_block', 'single_width_block',
        'hash', 'at_sign', 'reverse_video', 'ansi_colored', 'log_prefixed',
        'crlf', 'embedded_in_prose',
    ])
    def test_style_recovers_to_a_scannable_image(self, style):
        from lib.qr import terminal_qr_images
        imgs = terminal_qr_images(self._styles()[style])
        assert imgs, f'{style}: no QR recovered from terminal art'
        assert _decode(imgs[0]['uri']) == PAYLOAD, f'{style}: recovered QR does not scan'

    @pytest.mark.parametrize('modules,payload,border', [
        (21, 'short', 4),
        (33, PAYLOAD, 2),
        (49, 'https://login.example.com/device?user_code=WDJB-MJHT&sid=' + 'z' * 80, 4),
    ])
    def test_recovery_is_size_independent(self, modules, payload, border):
        """Symbols of different versions all recover — the grid maths must not
        be tuned to whichever size happened to be tested first. ``modules`` is
        the SYMBOL size (quiet zone excluded)."""
        from lib.qr import detect_terminal_qr_matrices, terminal_qr_images
        assert _symbol_modules(payload, border) == modules  # premise
        art = _cells(_qr_matrix(payload, border))
        assert len(detect_terminal_qr_matrices(art)[0]) == modules
        imgs = terminal_qr_images(art)
        assert imgs and _decode(imgs[0]['uri']) == payload

    def test_descriptor_shape_matches_the_render_contract(self):
        """The descriptor must carry the fields the timeline renderer reads,
        or the image is transported and then silently not drawn."""
        from lib.qr import terminal_qr_images
        d = terminal_qr_images(_cells(_qr_matrix()))[0]
        assert d['uri'].startswith('data:image/png;base64,')
        assert d['format'] == 'png'
        assert d['filename'].endswith('.png')
        assert d['source'] == 'terminal'

    def test_dark_module_as_blank_glyph_is_not_cropped_away(self):
        """Root-cause pin: in print_ascii a DARK module is the NBSP blank, so
        cropping the block to art-glyph columns truncated the symbol (a
        29-module QR arrived as 25 columns and was rejected). The recovered
        matrix must be the FULL symbol width."""
        from lib.qr import detect_terminal_qr_matrices
        art = _print_ascii('short', border=4)
        mats = detect_terminal_qr_matrices(art)
        assert mats, 'half-block art with NBSP dark modules was not recovered'
        assert len(mats[0]) == _symbol_modules('short', 4) == 21

    def test_inverted_quiet_zone_does_not_shift_the_grid(self):
        """Root-cause pin: an inverted symbol's quiet zone is DARK, so peeling
        only light borders mis-aligns the grid and the finders land
        off-corner."""
        from lib.qr import detect_terminal_qr_matrices, is_valid_qr_matrix
        mats = detect_terminal_qr_matrices(_print_ascii(invert=True))
        assert mats and is_valid_qr_matrix(mats[0])

    def test_two_codes_in_one_transcript_are_both_recovered(self):
        from lib.qr import terminal_qr_images
        a, b = 'https://first.example/x', 'https://second.example/y'
        art = (_cells(_qr_matrix(a)) + '\n\nthen:\n\n' + _cells(_qr_matrix(b)))
        imgs = terminal_qr_images(art)
        assert len(imgs) == 2
        assert {_decode(i['uri']) for i in imgs} == {a, b}
        assert imgs[0]['filename'] != imgs[1]['filename']


class TestNoFalsePositives:
    """Ordinary terminal output must never be mistaken for a QR — a bogus
    image in the timeline is worse than none."""

    @pytest.mark.parametrize('text', [
        'INFO starting\nERROR failed\n' * 20,
        '┌────┐\n│ hi │\n└────┘\n' * 6,
        '\n'.join('█' * (i % 40) for i in range(40)),          # progress bars
        '\n'.join('███ ███ ███' for _ in range(30)),            # block table
        '\n'.join('#' * (i % 5) + ' heading' for i in range(40)),  # markdown
        '\n'.join('▒▒▓▓' for _ in range(25)),                   # shade blocks
        '\n'.join('▄▄▄▄' for _ in range(30)),                   # spinner rows
        '',
    ])
    def test_non_qr_output_yields_nothing(self, text):
        from lib.qr import detect_terminal_qr_matrices
        assert detect_terminal_qr_matrices(text) == []

    def test_oversized_output_is_skipped_not_scanned(self):
        from lib.qr import detect_terminal_qr_matrices
        assert detect_terminal_qr_matrices('█' * (600 * 1024)) == []


class TestValidator:
    def test_rejects_wrong_dimensions_and_negatives(self):
        from lib.qr import is_valid_qr_matrix
        m = _qr_matrix()
        good = [[1 if v else 0 for v in r] for r in m]
        # the real symbol (quiet zone stripped) validates
        n = len(good)
        sym = [row[2:n - 2] for row in good[2:n - 2]]
        assert is_valid_qr_matrix(sym)
        # a photographic negative must NOT validate — that is the whole
        # mechanism by which polarity is derived instead of guessed
        assert not is_valid_qr_matrix([[1 - v for v in r] for r in sym])
        assert not is_valid_qr_matrix([])
        assert not is_valid_qr_matrix([[1, 0], [0, 1]])          # too small
        assert not is_valid_qr_matrix(sym[:-1])                   # not square


# ═══════════════════════════════════════════════════════════════════════
#  Seam 1 — run_command: QR rides the shared finalize chokepoint
# ═══════════════════════════════════════════════════════════════════════

class TestRunCommandSeam:
    def test_terminal_qr_is_attached_to_a_run_command_result(self):
        from lib.qr import _MIN_BLOCK_LINES  # noqa: F401  (module import sanity)
        from lib.tasks_pkg.executor._finalize import _attach_terminal_qr
        results = [{'toolName': 'run_command', 'command': 'gh auth login',
                    'output': 'Scan:\n' + _cells(_qr_matrix()), 'exitCode': '0'}]
        _attach_terminal_qr(results)
        assert results[0].get('qrImages'), 'no QR attached to run_command result'
        assert _decode(results[0]['qrImages'][0]['uri']) == PAYLOAD

    def test_plain_output_gets_no_qr_field(self):
        from lib.tasks_pkg.executor._finalize import _attach_terminal_qr
        results = [{'toolName': 'run_command', 'output': 'total 4\ndrwxr-xr-x'}]
        _attach_terminal_qr(results)
        assert 'qrImages' not in results[0]

    def test_unrelated_tools_are_not_scanned(self):
        """Only terminal-transcript tools are scanned; a file read that
        happens to contain block art is not a scan-to-login prompt."""
        from lib.tasks_pkg.executor._finalize import _attach_terminal_qr
        results = [{'toolName': 'read_files', 'output': _cells(_qr_matrix())}]
        _attach_terminal_qr(results)
        assert 'qrImages' not in results[0]

    def test_attach_never_breaks_the_round(self):
        """A malformed result list must not raise: the command's real output
        matters more than the QR extra."""
        from lib.tasks_pkg.executor._finalize import _attach_terminal_qr
        results = [None, 'a string', {'toolName': 'run_command'},
                   {'toolName': 'run_command', 'output': None}]
        _attach_terminal_qr(results)  # must not raise

    def test_qr_is_attached_before_the_event_is_emitted(self):
        """Ordering invariant: the descriptors must exist BEFORE the SSE event
        is handed to the consumer.

        Asserting ``event['results'][0]['qrImages']`` alone is VACUOUS — the
        event carries the same list OBJECT as the round, so a mutation applied
        after emission is still visible through both references and the test
        passes either way (verified: moving the attach call after
        ``append_event`` did not turn such an assertion red). The consumer that
        actually breaks is one that COPIES or serialises the payload as it goes
        out, which is what a real SSE sink does. So snapshot a deep copy at
        emit time and assert the QR was already in it."""
        import copy

        import lib.tasks_pkg.executor._finalize as fin
        snapshots = []
        orig = fin.append_event
        fin.append_event = lambda task, ev: snapshots.append(copy.deepcopy(ev))
        try:
            round_entry = {'query': 'gh auth login', 'toolCallId': 'tc1'}
            fin._finalize_tool_round(
                {'id': 't' * 8}, 3, round_entry,
                [{'toolName': 'run_command', 'output': _cells(_qr_matrix())}],
            )
        finally:
            fin.append_event = orig
        assert round_entry['results'][0].get('qrImages'), 'round lost the QR'
        assert snapshots, 'no event emitted'
        emitted = snapshots[0]['results'][0]
        assert emitted.get('qrImages'), (
            'the emitted event did not yet carry qrImages — the QR was '
            'attached after emission, so a copying/serialising consumer '
            '(the real SSE sink) delivers a round with no scannable code'
        )
        assert _decode(emitted['qrImages'][0]['uri']) == PAYLOAD


# ═══════════════════════════════════════════════════════════════════════
#  Seam 1b — the LIVE phase: the code must appear WHILE the command blocks
# ═══════════════════════════════════════════════════════════════════════

class TestLiveScanSeam:
    """The seam that makes scan-to-login actually usable.

    A device-code / QR login command PRINTS the code and then BLOCKS waiting
    for the scan. Recovering the QR only at ``_finalize_tool_round`` delivers
    the image after the authorization window has closed — or after the command
    timed out for lack of a scan — which is indistinguishable from not having
    the feature. These guards pin that the image exists DURING the wait.
    """

    def _art(self, payload=PAYLOAD):
        return _cells(_qr_matrix(payload))

    def _run_stream(self, chunks):
        """Drive the real run_command progress callback and collect events."""
        import lib.tasks_pkg.handlers.code_exec as ce
        events = []
        orig = ce.append_event
        ce.append_event = lambda task, ev: events.append(ev)
        try:
            round_entry = {'toolCallId': 'tc1', 'toolName': 'run_command'}
            cb = ce._make_run_command_progress_cb(
                {'id': 't' * 8}, 7, round_entry, 'gh auth login')
            for c in chunks:
                cb('stdout', c)
            cb.flush()
        finally:
            ce.append_event = orig
        return events, round_entry

    def test_qr_is_emitted_while_the_command_is_still_running(self):
        """The core acceptance criterion: a scannable image reaches the client
        from the PROGRESS path, with no tool_result / finalize involved."""
        events, round_entry = self._run_stream(
            ['Open this QR:\n', self._art(), '\nWaiting for you to scan...\n'])
        qr_evs = [e for e in events if e.get('qrImages')]
        assert qr_evs, (
            'no tool_progress event carried a QR — the code only becomes an '
            'image at finalize, which is after the login window has closed'
        )
        assert all(e['type'] == 'tool_progress' for e in qr_evs)
        assert _decode(qr_evs[0]['qrImages'][0]['uri']) == PAYLOAD
        # and it is stamped on the round so a reconnect replay restores it
        assert round_entry.get('qrImages')

    def test_same_code_is_emitted_once_not_per_chunk(self):
        """A chunk arrives every ~200ms and the art stays in the buffer for the
        whole wait. Without dedup the same multi-KB image would be re-emitted
        dozens of times and flood the SSE channel.

        Drives ``LiveQrScanner`` DIRECTLY rather than through the progress
        callback: that callback coalesces output (``_COALESCE_BYTES`` /
        ``_COALESCE_MS``), so a burst of trailing chunks collapses into ~2
        flushes and ``scan()`` is barely reached. A callback-level assertion
        therefore passes even with the dedup memory destroyed (verified — it
        was measuring the coalescer, not the dedup). Growth here is above
        ``_MIN_GROWTH`` every time, so the ONLY thing that can suppress a
        repeat is the signature memory."""
        from lib.qr import LiveQrScanner
        s = LiveQrScanner()
        buf = 'Scan:\n' + self._art() + '\nWaiting...\n'
        first = s.scan(buf)
        assert len(first) == 1, 'the code was not reported on first sight'
        repeats = 0
        for i in range(12):
            # grow well past _MIN_GROWTH each round so the throttle cannot be
            # the thing that stays quiet — the art is still in the buffer.
            buf += 'still waiting ' + 'x' * 1024 + '\n'
            repeats += len(s.scan(buf))
        assert repeats == 0, (
            f'the same QR was re-reported {repeats}x on later scans — dedup is '
            'broken and every poll would re-push a multi-KB image over SSE'
        )

    def test_progress_path_emits_one_event_for_one_code(self):
        """End-to-end complement to the scanner-level dedup test: the wire sees
        exactly one QR-bearing frame for one code."""
        chunks = ['Scan:\n', self._art(), '\nWaiting...\n'] + ['.'] * 40
        events, round_entry = self._run_stream(chunks)
        qr_evs = [e for e in events if e.get('qrImages')]
        assert len(qr_evs) == 1, f'QR re-emitted {len(qr_evs)}x on the wire'
        assert len(round_entry['qrImages']) == 1

    def test_a_second_distinct_code_is_still_reported(self):
        """Dedup must key on the code, not latch after the first one — a
        refreshed/expired login prints a NEW code that must also surface."""
        other = 'https://second.example/device?code=ZZZ'
        events, round_entry = self._run_stream(
            ['first:\n', self._art(), '\nexpired, new code:\n',
             self._art(other), '\nwaiting\n'])
        got = {_decode(d['uri']) for d in round_entry.get('qrImages', [])}
        assert got == {PAYLOAD, other}

    def test_plain_chatty_output_emits_no_qr_and_stays_cheap(self):
        """The overwhelmingly common case: a noisy build log must not trigger
        detection work or attach anything."""
        events, round_entry = self._run_stream(
            [f'compiling module {i}\n' for i in range(200)])
        assert not [e for e in events if e.get('qrImages')]
        assert 'qrImages' not in round_entry

    def test_scanner_skips_until_the_buffer_can_hold_a_symbol(self):
        """Cost guard: re-scanning the whole buffer on every chunk is O(n^2)
        over a long command. Sub-threshold growth must not trigger a scan."""
        from lib.qr import LiveQrScanner
        s = LiveQrScanner()
        assert s.scan('x') == []
        assert s.scan('no art here\n' * 3) == []

    def test_scanner_is_resilient_to_garbage(self):
        from lib.qr import LiveQrScanner
        s = LiveQrScanner()
        assert s.scan('') == []
        assert s.scan('\x00\x01\x02' * 500) == []

    def test_signature_distinguishes_codes(self):
        from lib.qr import qr_signature, terminal_qr_images
        a = terminal_qr_images(self._art())[0]
        b = terminal_qr_images(self._art('https://other.example/x'))[0]
        assert qr_signature(a) and qr_signature(a) != qr_signature(b)
        assert qr_signature({}) == ''


# ═══════════════════════════════════════════════════════════════════════
#  Seam 2 — ask_human: the blocking scan-to-login prompt
# ═══════════════════════════════════════════════════════════════════════

class TestAskHumanSeam:
    def test_question_embeds_a_served_url_not_base64(self, tmp_path, monkeypatch):
        """Base64 in the question would be shipped to the translation API as
        prose when autoTranslate is on (stream_lifecycle.js) and come back
        mangled. A short sentence + path URL survives that round trip."""
        monkeypatch.setattr('lib.runtime_paths.uploads_root', lambda: str(tmp_path))
        from lib.qr import qr_login_question
        q = qr_login_question(PAYLOAD, prompt='Scan to log in')
        assert 'base64' not in q
        assert '](/api/images/' in q
        assert q.startswith('Scan to log in')

    def test_the_referenced_file_exists_and_scans(self, tmp_path, monkeypatch):
        """The prompt must not reference a code that isn't there."""
        monkeypatch.setattr('lib.runtime_paths.uploads_root', lambda: str(tmp_path))
        from lib.qr import qr_login_question
        q = qr_login_question(PAYLOAD)
        name = q.split('](/api/images/')[1].split(')')[0]
        path = tmp_path / 'images' / name
        assert path.exists()
        uri = 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode()
        assert _decode(uri) == PAYLOAD

    def test_tool_description_advertises_the_capability(self):
        """The model cannot use an undocumented affordance. If the schema stops
        mentioning it, ask_human silently loses the QR use case."""
        from lib.tools.human_guidance import ASK_HUMAN_TOOL
        desc = ASK_HUMAN_TOOL['function']['parameters']['properties']['question']['description']
        assert 'MARKDOWN' in desc
        assert 'qr_login_question' in desc
