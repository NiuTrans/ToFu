"""`lib.log_clean` collapse markers must not claim hardware they cannot see.

WHY THIS EXISTS
---------------
``_DEVICE_ID_PATTERNS`` matches ``worker`` / ``rank`` / ``device`` in the same
list as ``cuda`` / ``gpu``. For FINGERPRINTING that is correct — each is just
"a number that varies between otherwise-identical lines". But the collapse
summary then rendered the group as ``×N devices``, which is an assertion about
hardware that the input does not support. Measured end-to-end through
``detect_log_noise``:

  * 8 ``DataLoader worker N`` lines (no GPU word anywhere) → ``×8 devices: 0-7``
  * 3 ``postgres: io worker N`` processes → counted as 3 "devices"

A numbered variant is not a device.

SEVERITY (deliberate, do not escalate)
--------------------------------------
This is the same FAMILY as the ``command_analysis`` fold defect but one grade
milder: ``_format_device_range`` here emits bare indices with **no ``cuda:``
prefix**, so it never produced the concrete false fact "three postgres
processes are three GPUs". Only the word ``devices`` was unearned. The fix is
therefore neutral wording — NOT the accelerator/ordinal/no-evidence tiering
that command_analysis needed, because there is no ``cuda:`` label here to
justify.

CORPUS
------
``tests/fixtures/real_worker_progress_log.txt`` is a REAL captured user log
(interleaved multi-worker progress bars with genuine ``cuda:N`` tokens),
vendored from ``debug/test_log_cleanup.py`` whose docstring records it as "the
user's exact example". It lives under ``tests/fixtures/`` because ``debug/`` is
gitignored — reading it from there made this suite SKIP silently in any clean
checkout, i.e. report green while never running. A missing corpus is now a hard
failure, not a skip.

Note on scope, checked before writing these tests: none of the 24 live files
under ``logs/`` contains a numbered-variant fold case at all (they fold
heavily — vendor.log collapses 5,633 lines — but always without indices), so
the captured training-style sample is the only real corpus available for this
specific path. That is stated rather than papered over with a synthetic one.

DISCIPLINE
----------
Assertions are on the RENDERED marker (a behaviour), never on a constant or a
regex table, so retuning the patterns cannot cause a false red while deleting
the neutral wording turns these red immediately.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.log_clean import detect_log_noise  # noqa: E402
from lib.log_clean._collapse import (  # noqa: E402
    _collapse_progress_bars,
    _collapse_similar_lines,
)
from lib.log_clean._helpers import (  # noqa: E402
    _extract_device_ids,
    _fingerprint,
)

pytestmark = pytest.mark.unit

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_SAMPLE_FILE = os.path.join(
    _REPO, 'tests', 'fixtures', 'real_worker_progress_log.txt')

# Accelerator words that WOULD justify naming hardware, if we named any.
_ACCEL_RE = re.compile(r'cuda|nvidia|\bgpu\b', re.IGNORECASE)


def _real_captured_sample():
    """Load the captured multi-worker training log.

    Originally read straight out of ``debug/test_log_cleanup.py``, but ``debug/``
    is gitignored: in a clean checkout that made these tests SKIP silently and
    forever — a guard that reports green because it never ran. The artefact is
    therefore vendored under ``tests/fixtures/`` and a missing corpus is a hard
    FAILURE, never a skip.
    """
    if not os.path.exists(_REAL_SAMPLE_FILE):
        pytest.fail(
            f'Real-log corpus missing: {_REAL_SAMPLE_FILE}. It is a captured '
            'user log (provenance in the file header) and must stay in the '
            'repo — do NOT replace it with a synthesised sample.')
    text = open(_REAL_SAMPLE_FILE, encoding='utf-8', errors='replace').read()
    # Strip the provenance header comment; keep the log verbatim.
    return '\n'.join(ln for ln in text.split('\n')
                     if not ln.startswith('# '))


def _worker_lines(n=8):
    """Multi-worker startup lines with NO accelerator token at all.

    This exact shape is what production emits for dataloaders / process pools;
    it is the case that rendered as "×8 devices" with zero GPUs involved.
    """
    return [
        f'[INFO] DataLoader worker {i} started, prefetch_factor=2, pin_memory=True'
        for i in range(n)
    ]


def _markers(text):
    return [ln.strip() for ln in text.split('\n') if '…' in ln]


# ══════════════════════════════════════════════════════════
#  Scan surface — prove the corpus reaches the collapse path
# ══════════════════════════════════════════════════════════

def test_scan_surface_report(capsys):
    """Print what each corpus actually trips before any assertion relies on it."""
    workers = _worker_lines()
    out_w, n_w = _collapse_similar_lines(workers)
    sample = _real_captured_sample()
    res_s = detect_log_noise(sample)
    txt_s = getattr(res_s, 'cleanedText', '') or ''

    print('\n--- scan surface ---')
    print(f'worker corpus   : {len(workers)} lines, collapsed={n_w}, '
          f'markers={len(_markers(chr(10).join(out_w)))}, '
          f'ids={_extract_device_ids(workers)}, '
          f'accel_words={bool(_ACCEL_RE.search(chr(10).join(workers)))}')
    print(f'captured sample : {len(sample.splitlines())} lines from '
          f'debug/test_log_cleanup.py, markers={len(_markers(txt_s))}, '
          f'accel_words={bool(_ACCEL_RE.search(sample))}')
    print(f'distinct fingerprints in worker corpus: '
          f'{len({_fingerprint(l) for l in workers})} (1 == they group)')

    # Each corpus must genuinely exercise the path, else everything below is
    # vacuous — the failure mode this repo has logged repeatedly.
    assert n_w > 0, 'worker corpus did not collapse; it cannot test the marker'
    assert len(_extract_device_ids(workers)) > 1, (
        'worker corpus yields <2 ids, so the index-spread branch never runs')
    assert not _ACCEL_RE.search('\n'.join(workers)), (
        'worker corpus must contain NO accelerator word to prove the negative')


# ══════════════════════════════════════════════════════════
#  The contract
# ══════════════════════════════════════════════════════════

def test_numbered_workers_are_not_called_devices():
    """8 dataloader workers must not be reported as 8 devices."""
    out, _ = _collapse_similar_lines(_worker_lines())
    text = '\n'.join(out)
    assert 'device' not in text.lower(), (
        'Collapse marker calls numbered workers "devices" though the input '
        'contains no accelerator token at all — an unearned hardware claim.\n'
        'Marker(s): ' + repr(_markers(text)))


def test_the_index_spread_is_still_reported():
    """Complement: dropping the claim must not drop the INFORMATION.

    Without this, deleting the whole annotation would satisfy the assertion
    above while making the summary less useful than before.
    """
    out, _ = _collapse_similar_lines(_worker_lines())
    markers = _markers('\n'.join(out))
    assert markers, 'no collapse marker emitted'
    joined = ' '.join(markers)
    assert '0-7' in joined, (
        f'index spread lost from the marker; got {markers!r}')
    assert '8' in joined, f'variant count lost from the marker; got {markers!r}'


def test_progress_bar_collapse_makes_no_hardware_claim():
    """The tqdm path had the same wording defect and the same fix."""
    lines = []
    for pct in (0, 10, 20, 30, 40, 50):
        for w in range(4):
            lines.append(
                f'Ingest worker {w}: {pct}%|##   | {pct}/100 '
                f'[00:0{w}<00:10, 9.9it/s]')
    out, collapsed = _collapse_progress_bars(lines)
    assert collapsed > 0, 'fixture did not reach the progress-bar collapse'
    text = '\n'.join(out)
    assert 'device' not in text.lower(), (
        'Progress-bar summary claims devices with no accelerator token.\n'
        'Marker(s): ' + repr(_markers(text)))


def test_end_to_end_through_the_public_entrypoint():
    """Drive detect_log_noise, not just the private pass.

    Guards the wiring: fixing the helper but leaving a caller un-migrated
    would keep the defect live on the route that users actually hit
    (routes/api_v1/logs.py → the log-noise banner).
    """
    res = detect_log_noise('\n'.join(_worker_lines()))
    text = getattr(res, 'cleanedText', '') or ''
    assert text, 'entrypoint produced no cleaned text'
    assert 'device' not in text.lower(), (
        'The user-visible cleanup path still claims devices: '
        + repr(_markers(text)))


def test_real_captured_log_sample_makes_no_unearned_claim():
    """The captured user log — real GPU tokens present, still no false claim.

    This sample DOES contain cuda:N, so it is the case where naming hardware
    would arguably be defensible; the module still must not, because it has no
    evidence-gated label and its numbers come from a mixed pattern list.
    """
    sample = _real_captured_sample()
    assert _ACCEL_RE.search(sample), (
        'captured sample no longer contains accelerator tokens; it can no '
        'longer exercise this case')
    res = detect_log_noise(sample)
    text = getattr(res, 'cleanedText', '') or ''
    produced = [m for m in _markers(text) if 'device' in m.lower()]
    assert not produced, (
        'Collapse markers generated by this run claim devices: '
        + repr(produced))
