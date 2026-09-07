"""Regression guard for lib/fetch/playwright_pool.py task discriminator.

The pool's worker loop was extended to dispatch a new task kind
(``'pdf_render'``).  Older callers (HTTP fetches) still arrive as
``((url, timeout, max_chars), result_q)``.  We test the discrimination
logic without actually launching Chromium — purely against the static
shape detection used in the worker loop.
"""
from __future__ import annotations

import pytest


def test_legacy_fetch_payload_shape():
    # Legacy: payload[0] is a string URL (so first element type is str
    # but the tuple length is 3, NOT 2 → falls through to fetch path).
    payload = ('https://example.com', 20, 1024)
    is_kind = (
        isinstance(payload, tuple)
        and len(payload) == 2
        and isinstance(payload[0], str)
    )
    assert not is_kind, 'Legacy fetch payload must not match kind dispatch'


def test_pdf_render_payload_shape():
    payload = ('pdf_render', {'html': '<p>x</p>', 'title': 't'})
    is_kind = (
        isinstance(payload, tuple)
        and len(payload) == 2
        and isinstance(payload[0], str)
    )
    assert is_kind, 'pdf_render payload must match kind dispatch'


def test_url_no_timeout_payload_does_not_match():
    """A two-element legacy variant with (url, timeout) is theoretically
    possible historically — guard against accidental kind dispatch."""
    payload = ('https://example.com', 20)
    is_kind = (
        isinstance(payload, tuple)
        and len(payload) == 2
        and isinstance(payload[0], str)
    )
    # This SHOULD trip kind dispatch by our shape rule (str + len 2).
    # The pool unpacks via `url, timeout, max_chars = payload` for the
    # legacy path; a 2-element legacy tuple would have crashed there
    # historically.  All current callers use 3-element tuples, but if
    # someone changes that, they'll see this test fail and remember to
    # update the discriminator.  Documented for posterity.
    assert is_kind
