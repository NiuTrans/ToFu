"""tests/test_command_fold_honesty.py — the run_command output folder must
describe what it actually did.

WHY THIS EXISTS
===============
``_clean_command_output`` groups CONSECUTIVE lines that share a structural
fingerprint (``_line_fingerprint`` normalises digit runs to ``#``) and
replaces each group with its first line plus a marker. Two measured defects
motivated this guard:

  1. The marker said ``(and N more similar lines)``. On real ``ps aux``
     output every folded group was 100% DISTINCT (different PIDs, ports,
     RSS, times) — 7 groups, 7 with zero duplicates. Calling those "similar"
     misdescribes the result to the only reader that matters, the model.

  2. Worse: ``_extract_device_ids`` matched ``worker``/``rank`` in the SAME
     regex as ``cuda``/``gpu``, so real lines like
     ``postgres: io worker 0/1/2`` rendered as ``… (×3 devices on cuda:0-2) …``
     — three database processes reported as three GPUs, with the original
     lines replaced. That is an invented fact, not a lossy summary, and it is
     the exact opposite of "help the model spot real problems".

CORPUS DISCIPLINE
=================
Fixtures are REAL captured command output (``tests/fixtures/real_ps_aux.txt``,
``real_ls_la_tests.txt``), never synthesised template strings. A synthetic
corpus of near-identical lines (``file_0000_nnnn.txt`` … ``file_2999_nnnn.txt``)
compresses ~99.9% because only the digits differ, while REAL directory
listings compress 0.3% — measuring the folder on such a corpus reports the
construction trick, not production behaviour.

Boundary note (checked, do not "simplify" away): ``_line_fingerprint``
returns None for lines under 20 chars, so short lines never fold at all. The
guard asserts its corpus actually clears that bar — otherwise it could pass
while exercising nothing, the failure mode this repo has logged repeatedly.

These are BEHAVIOUR assertions on returned text, not assertions about which
regex or constant the implementation uses; a reasonable rewrite that keeps
the output honest stays green.
"""

import os
import re
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.project_mod.command_analysis import (  # noqa: E402
    _clean_command_output,
    _extract_accelerator_ids,
    _extract_progress_label,
    _line_fingerprint,
)

pytestmark = pytest.mark.unit

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, 'fixtures')

PS_AUX = os.path.join(FIXTURES, 'real_ps_aux.txt')
LS_LA = os.path.join(FIXTURES, 'real_ls_la_tests.txt')

# Any marker the folder emits, plus the aggregate footer.
_MARKER_RE = re.compile(r'^\s*…\s*\(.*\)\s*…\s*$')
_FOOTER_PREFIX = '[output folded:'


def _read(path):
    if not os.path.exists(path):
        pytest.fail(
            f'Real-output corpus missing: {path}. This guard MUST run against '
            f'captured command output; regenerate with `ps aux > {PS_AUX}` / '
            f'`ls -la tests/ > {LS_LA}`. Do NOT substitute a synthetic '
            'template corpus — it measures the construction, not the folder.'
        )
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _fold_groups(text):
    """Reproduce the folder's grouping to find what it WOULD collapse.

    Uses the shipped ``_line_fingerprint`` (never a hand-copied duplicate of
    it) so this helper cannot drift from production grouping.
    """
    lines = text.split('\n')
    groups = []
    i = 0
    while i < len(lines):
        fp = _line_fingerprint(lines[i])
        if fp is None:
            i += 1
            continue
        j = i + 1
        while j < len(lines) and _line_fingerprint(lines[j]) == fp:
            j += 1
        if j - i > 2:
            groups.append(lines[i:j])
        i = j if j > i else i + 1
    return groups


# ══════════════════════════════════════════════════════════
#  Corpus sanity — verify the SCAN SURFACE before asserting
# ══════════════════════════════════════════════════════════

def test_corpus_actually_reaches_the_folding_path():
    """A corpus of sub-20-char lines would make every assertion below vacuous."""
    for path in (PS_AUX, LS_LA):
        text = _read(path)
        eligible = [ln for ln in text.split('\n')
                    if _line_fingerprint(ln) is not None]
        assert len(eligible) > 100, (
            f'{os.path.basename(path)}: only {len(eligible)} lines clear the '
            '20-char fingerprint floor — this corpus cannot exercise folding.'
        )

    groups = _fold_groups(_read(PS_AUX))
    assert groups, (
        'ps aux corpus produces no fold group larger than 2 — nothing folds, '
        'so the honesty assertions would pass trivially.'
    )


# ══════════════════════════════════════════════════════════
#  1. Distinct lines must not be called "similar"
# ══════════════════════════════════════════════════════════

def test_folded_lines_that_all_differ_are_not_called_similar():
    text = _read(PS_AUX)
    groups = _fold_groups(text)

    all_distinct = [g for g in groups
                    if len({ln.strip() for ln in g}) == len(g)]
    assert all_distinct, (
        'Expected at least one fold group whose lines are all distinct — '
        'that is the case the wording must not misdescribe.'
    )

    out = _clean_command_output(text)
    assert 'similar' not in out.lower(), (
        'Fold marker claims the folded lines are "similar", but on this real '
        f'ps aux output {len(all_distinct)} group(s) are 100% distinct '
        '(different PIDs/ports/RSS). Describe the grouping rule (shared '
        'structure, differing values) instead of asserting similarity.\n'
        f'Sample group of {len(all_distinct[0])} all-different lines:\n  '
        + '\n  '.join(ln[:100] for ln in all_distinct[0][:3])
    )


def test_marker_states_the_grouping_rule():
    """The replacement text must say WHY lines were grouped."""
    out = _clean_command_output(_read(PS_AUX))
    markers = [ln for ln in out.split('\n') if _MARKER_RE.match(ln)]
    assert markers, 'No fold marker emitted on a corpus that does fold.'
    assert any(('structure' in m or 'variants' in m or 'devices' in m)
               for m in markers), (
        'Fold markers name no grouping basis: ' + repr(markers[:3])
    )


# ══════════════════════════════════════════════════════════
#  2. No accelerator claim without accelerator evidence
# ══════════════════════════════════════════════════════════

def test_no_cuda_claim_when_the_output_has_no_accelerator():
    """`postgres: io worker 0/1/2` must not become `cuda:0-2`."""
    text = _read(PS_AUX)
    # The precondition is specifically "no accelerator word FOLLOWED BY AN
    # INDEX" — that pairing is what could legitimately licence a cuda: range.
    # A bare flag such as chromium's `--disable-gpu` carries no index and so
    # cannot; an earlier version of this check banned the bare word too and
    # failed on a browser process, which would have been a false accusation
    # against the corpus. Judged with the SHIPPED extractor so the guard and
    # production cannot disagree about what counts as evidence.
    accel = _extract_accelerator_ids(text.split('\n'))
    assert not accel, (
        'This corpus was chosen because it contains no INDEXED accelerator '
        f'(needed to prove the negative), but found ids {accel}. Recapture on '
        'a process list with no cuda:N / gpu N processes.'
    )

    out = _clean_command_output(text)
    assert 'cuda' not in out.lower(), (
        'Folder emitted a cuda: device range for output containing no GPU at '
        'all — the numbered `worker N` processes were misattributed as '
        'accelerators. A marker that invents hardware is worse than no '
        'marker.\nOffending line(s): '
        + repr([ln for ln in out.split('\n') if 'cuda' in ln.lower()][:3])
    )


def test_progress_bars_do_not_invent_devices_from_duplicate_percentages():
    """Phase 3 defect, same family as the cuda: one but a different code path.

    The tqdm-group summariser inferred a device COUNT from "how many lines
    share a percentage", so four progress bars from a single-process loader
    were reported as '×2 devices' with no device word anywhere in the input.
    Concurrency is a real observation; hardware is not — the marker may
    describe the shape but must not name what runs it.
    """
    lines = [
        'Ingesting records: 10%|##        | 10/100 [00:01<00:09, 9.99it/s]',
        'Ingesting records: 50%|#####     | 50/100 [00:05<00:05, 9.99it/s]',
        'Ingesting records: 50%|#####     | 50/100 [00:05<00:05, 9.98it/s]',
        'Ingesting records: 99%|######### | 99/100 [00:09<00:00, 9.97it/s]',
    ]
    raw = '\n'.join(lines) + '\n'
    assert not _extract_accelerator_ids(lines), 'fixture must carry no device'

    out = _clean_command_output(raw)
    assert 'device' not in out.lower(), (
        'Progress-bar summary claims devices for output with no device word '
        'at all — the count came from duplicate percentages, which only shows '
        'concurrency.\nGot: '
        + repr([ln for ln in out.split('\n') if 'device' in ln.lower()])
    )


def test_progress_bars_with_real_accelerators_keep_the_device_range():
    """Complement: removing Phase 3's accelerator branch must also fail.

    Fixture note (I got this wrong once): the accelerator token must sit in
    the SHORT trailing segment, not in the label. ``_extract_progress_label``
    keys the group on the text BEFORE the bar, so ``cuda:0 train: …`` gives
    every line a different label and nothing groups — the test would have
    failed against correct production code. Trailing text over 20 chars is
    also rejected as an "announcement" line, so it must stay short.
    """
    lines = [
        f'Loading shards: 50%|#####     | 50/100 [00:05<00:05, 9.9it/s] cuda:{i}'
        for i in range(4)
    ]
    assert len({_extract_progress_label(ln) for ln in lines}) == 1, (
        'fixture broken: lines must share one progress label to form a group'
    )
    out = _clean_command_output('\n'.join(lines) + '\n')
    assert 'cuda:0-3' in out, (
        'Genuine multi-GPU progress bars lost their device range: ' + repr(out)
    )


def test_real_accelerator_output_still_gets_its_device_range():
    """The complement: genuine GPU logs must KEEP the cuda: attribution.

    Without this, deleting the accelerator branch entirely would satisfy the
    negative assertion above and the suite would stay green.
    """
    lines = [
        f'[Trainer] loading shard onto cuda:{i} — allocating pinned buffers'
        for i in range(4)
    ]
    out = _clean_command_output('\n'.join(lines) + '\n')
    assert 'cuda:0-3' in out, (
        'Genuine cuda:0..3 lines lost their device-range attribution: '
        + repr(out)
    )


# ══════════════════════════════════════════════════════════
#  3. The fold total must reach the model
# ══════════════════════════════════════════════════════════

def test_folded_output_reports_how_many_lines_were_omitted():
    text = _read(PS_AUX)
    out = _clean_command_output(text)

    footer = [ln for ln in out.split('\n') if ln.startswith(_FOOTER_PREFIX)]
    assert footer, (
        'Folded output carries no aggregate count, so the model cannot tell '
        'whether it received the whole output or a fraction of it. The total '
        'currently exists only in logger.debug, which the model never sees.'
    )

    omitted = int(re.search(r'(\d+) of', footer[0]).group(1))
    expected = sum(len(g) - 1 for g in _fold_groups(text))
    assert omitted == expected, (
        f'Reported {omitted} omitted lines but grouping accounts for '
        f'{expected} — the number the model reads must be the real one.'
    )


def test_unfolded_output_is_returned_byte_identical():
    """No footer noise when nothing was folded (real ls -la barely folds)."""
    plain = 'total 4\ndrwxr-xr-x 2 user group 4096 Jan  1 00:00 .\n'
    assert _clean_command_output(plain) == plain

    text = _read(LS_LA)
    out = _clean_command_output(text)
    if _FOOTER_PREFIX not in out:
        assert 'similar' not in out.lower()


def test_live_command_output_is_described_honestly_end_to_end():
    """Drive a real subprocess, not just a fixture, so the wiring is covered."""
    proc = subprocess.run(['ps', 'aux'], capture_output=True, text=True,
                          timeout=60)
    raw = proc.stdout
    if not raw.strip():
        pytest.skip('ps aux produced no output in this environment')
    out = _clean_command_output(raw)
    assert 'similar' not in out.lower()
    if not _extract_accelerator_ids(raw.split('\n')):
        assert 'cuda' not in out.lower()
