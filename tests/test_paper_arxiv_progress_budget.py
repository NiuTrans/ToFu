"""The arXiv SSE bridge retains only one newest parser progress update."""

from __future__ import annotations

import queue

import pytest

from routes.paper_pkg._arxiv import _offer_latest_progress


pytestmark = pytest.mark.unit


def test_progress_slot_coalesces_a_disconnected_consumers_backlog():
    progress_queue = queue.Queue(maxsize=1)

    for page in range(1, 10_001):
        _offer_latest_progress(
            progress_queue, ('progress', 'text', page, 10_000))

    assert progress_queue.qsize() == 1
    assert progress_queue.get_nowait() == (
        'progress', 'text', 10_000, 10_000)


def test_done_replaces_obsolete_progress_without_blocking():
    progress_queue = queue.Queue(maxsize=1)
    _offer_latest_progress(progress_queue, ('progress', 'text', 7, 100))
    _offer_latest_progress(progress_queue, ('done', None, None, None))

    assert progress_queue.qsize() == 1
    assert progress_queue.get_nowait() == ('done', None, None, None)
