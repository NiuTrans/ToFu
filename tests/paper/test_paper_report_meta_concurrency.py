"""Concurrent paper-report metadata updates are atomic in the Sidecar."""

from __future__ import annotations

import concurrent.futures
import time

import pytest

from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport
from lib.storage.errors import StorageError


pytest_plugins = ('tests._artifact_sidecar',)
pytestmark = pytest.mark.unit
OWNER_USER_ID = 1


def test_concurrent_meta_mutations_preserve_every_sibling_update():
    repository = PaperArtifactRepository(OWNER_USER_ID)
    paper_hash = f'meta-race-{time.time_ns()}'
    assert repository.put_report(
        PaperReport(paper_hash, 'en', 'body', meta={}),
        command_id=f'meta-race-create:{paper_hash}',
    )

    workers = 12

    def write(index):
        return PaperArtifactRepository(OWNER_USER_ID).merge_report_second_pass(
            paper_hash,
            'en',
            f'worker-{index}',
            {'index': index},
            command_id=f'meta-race-worker:{paper_hash}:{index}',
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(write, range(workers)))
    assert all(isinstance(result, dict) for result in results)

    report = repository.get_report(paper_hash, 'en')
    assert report is not None
    assert report.meta['secondPasses'] == {
        f'worker-{index}': {'index': index} for index in range(workers)
    }


def test_conflicting_command_replay_leaves_committed_metadata_unchanged():
    repository = PaperArtifactRepository(OWNER_USER_ID)
    paper_hash = f'meta-conflict-{time.time_ns()}'
    assert repository.put_report(
        PaperReport(paper_hash, 'en', 'body', meta={'stable': True}),
        command_id=f'meta-conflict-create:{paper_hash}',
    )
    command_id = f'meta-conflict-command:{paper_hash}'
    repository.merge_report_second_pass(
        paper_hash,
        'en',
        'insight',
        {'value': 'committed'},
        command_id=command_id,
    )

    with pytest.raises(StorageError):
        repository.merge_report_second_pass(
            paper_hash,
            'en',
            'insight',
            {'value': 'must-not-commit'},
            command_id=command_id,
        )

    report = repository.get_report(paper_hash, 'en')
    assert report is not None
    assert report.meta['stable'] is True
    assert report.meta['secondPasses']['insight'] == {'value': 'committed'}
