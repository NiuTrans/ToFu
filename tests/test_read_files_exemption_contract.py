"""The ``read_files`` budget exemption, pinned as behaviour rather than prose.

WHY THIS FILE EXISTS
--------------------
``_BUDGET_EXEMPT_TOOLS`` used to justify itself in a comment: "these tools
already have their own internal limits (MAX_READ_CHARS=100K per file,
BATCH_CHAR_BUDGET=200K)".  Both numbers were wrong — the real values are
1_000_000 (above the 512 KB file gate, so it never fires) and 50 MB.  The
exemption was therefore resting on two knives that do not cut, and the comment
made that invisible to anyone reading the code.

So these tests assert the RESULT — what a caller actually gets back — and never
a constant's value.  Per the charter's behaviour-guard rule: an assertion that
would stop being true if the implementation were reasonably rewritten is
testing the implementation, not the contract.  Re-tune ``MAX_READ_CHARS``, the
batch budget, or the ceiling arithmetic freely; these still hold, and if the
exemption is ever silently dropped they go red.

NEUTER VERIFICATION (both confirmed to bite):
  * Remove ``'read_files'`` from ``_BUDGET_EXEMPT_TOOLS``
    → ``test_exempt_tool_result_is_returned_whole`` fails.
  * Collapse ``clamp_tool_result_text``'s two-message split back to the single
    binary/base64 wording
    → ``test_oversized_text_read_is_not_accused_of_leaking_binary`` fails.
"""

import pytest

from lib.tasks_pkg.compaction._budget import (
    budget_tool_result,
    clamp_tool_result_text,
)
from lib.tasks_pkg.compaction._constants import (
    _SINGLE_RESULT_HARD_CEILING_CHARS,
)

pytestmark = pytest.mark.unit


def _prose(n_chars: int) -> str:
    """Text-shaped filler: whitespace-broken, like source or logs."""
    line = 'def handler(request):  # a line of ordinary source code\n'
    return (line * (n_chars // len(line) + 1))[:n_chars]


def _base64ish(n_chars: int) -> str:
    """Blob-shaped filler: one unbroken run, like base64 or decoded binary."""
    return 'QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVowMTIzNDU2Nzg5' * (n_chars // 48 + 1)


# ---------------------------------------------------------------- exemption

def test_exempt_tool_result_is_returned_whole():
    """A read result far past any per-tool budget comes back byte-identical.

    This is the exemption's real content: no ceiling on THIS layer.  Asserted
    as identity of the returned string, so it survives any re-tuning of the
    budget table.
    """
    body = _prose(600_000)
    out = budget_tool_result('read_files', body, 'tc_exempt', 'conv_exempt')
    assert out == body, (
        'read_files is budget-exempt: its result must not be truncated, '
        'persisted, or annotated by the budget layer. '
        f'Got {len(out):,} chars back from {len(body):,} in.'
    )


def test_exemption_ceiling_is_the_documented_backstop_only():
    """Below the hard ceiling nothing touches the result; above it, it clamps.

    Pins the ONE bound that genuinely applies to read_files, by probing either
    side of it rather than reading the constant into an equality assertion.
    """
    under = _prose(_SINGLE_RESULT_HARD_CEILING_CHARS - 10_000)
    assert clamp_tool_result_text('read_files', under) == under, (
        'Content under the hard ceiling must pass through untouched.'
    )

    over = _prose(_SINGLE_RESULT_HARD_CEILING_CHARS + 500_000)
    clamped = clamp_tool_result_text('read_files', over)
    assert len(clamped) < len(over), (
        'The hard ceiling is the only bound that applies to read_files — '
        'it must still be enforced.'
    )


# ------------------------------------------------- hard-ceiling attribution

def test_oversized_text_read_is_not_accused_of_leaking_binary():
    """A legitimate 10 MB batch read must not be told it leaked binary data.

    The regression this guards: the model performs a normal multi-file read,
    trips the ceiling, and receives a precisely-worded but FALSE diagnosis of
    its own behaviour ("binary/base64 data leaked into a text result").  A
    wrong explanation is worse than none — it is the opposite of helping the
    model recognise real problems.
    """
    out = clamp_tool_result_text('read_files', _prose(10 * 1024 * 1024))

    assert 'binary' not in out and 'base64' not in out, (
        'Plain text tripping the ceiling was accused of leaking binary data. '
        f'Message was: {out[560_000:560_400]!r}'
    )
    assert 'start_line' in out, (
        'The message must tell the model how to recover — request fewer paths '
        'or a specific line range.'
    )
    assert 'single-result limit' in out, (
        'The message must name the limit that was exceeded.'
    )


def test_actual_blob_leak_still_gets_the_blob_diagnosis():
    """The negative half: a real base64 leak keeps the investigate-me wording.

    Without this, the split could be "fixed" by deleting the blob branch
    entirely and the suite would stay green while the genuinely useful
    diagnosis disappeared.
    """
    out = clamp_tool_result_text('read_files', _base64ish(2 * 1024 * 1024))

    assert 'binary/base64' in out, (
        'An unbroken multi-megabyte run is a blob leak and must still be '
        'reported as one.'
    )


def test_attribution_follows_shape_not_tool_name():
    """Same tool, two shapes, two messages — the branch keys on content.

    A blob can leak through any tool and any tool can legitimately return a
    lot of text, so tool identity cannot decide this.  Asserting both
    directions through ONE tool name is what makes that concrete.
    """
    text_msg = clamp_tool_result_text('run_command', _prose(2 * 1024 * 1024))
    blob_msg = clamp_tool_result_text('run_command', _base64ish(2 * 1024 * 1024))

    assert 'binary' not in text_msg
    assert 'binary/base64' in blob_msg
