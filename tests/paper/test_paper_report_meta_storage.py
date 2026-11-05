"""Report metadata projection agrees across real SQL and native authorities."""

from contextlib import ExitStack

import pytest

from lib.paper.artifact_repository import PaperArtifactRepository, PaperReport


pytestmark = pytest.mark.api


@pytest.fixture(params=['sqlite', 'postgres', 'tofudb',
                        pytest.param('tofudb_embedded', marks=pytest.mark.serial)])
def metadata_authority(request, monkeypatch, tmp_path):
    from lib.storage import StorageSupervisor
    from tests.support.postgres_cluster import owned_postgres_cluster

    backend = request.param
    monkeypatch.setenv('TOFUDB_OWNER_ID', '17')
    monkeypatch.setenv('TOFUDB_TENANT_ID', '1')
    monkeypatch.setenv('TOFUDB_TRANSPORT',
                       'embedded' if backend == 'tofudb_embedded' else 'daemon')
    with ExitStack() as resources:
        if backend == 'postgres':
            dsn = resources.enter_context(owned_postgres_cluster())
            path = tmp_path / 'postgres-dsn'
            path.write_text(dsn)
            monkeypatch.setenv('TOFU_STORAGE_TEST_POSTGRES_DSN_FILE', str(path))
        supervisor = StorageSupervisor(
            project_root=tmp_path / 'authority',
            backend='tofudb' if backend == 'tofudb_embedded' else backend)
        try:
            supervisor.start()
            yield supervisor, PaperArtifactRepository(
                17, client_factory=lambda **_: supervisor.client)
        finally:
            supervisor.stop()


def test_replacing_the_last_cost_with_zero_clears_prior_totals(metadata_authority):
    supervisor, repository = metadata_authority
    assert repository.put_report(
        PaperReport('paper-a', 'en', 'unchanged body', meta={'promptTokens': 10}),
        command_id='create-report')
    first = repository.merge_report_second_pass(
        'paper-a', 'en', 'insight',
        {'usage': {'prompt_tokens': 3}, 'costCny': 2, 'costUsd': 0.5},
        command_id='initial-pass')
    assert first['totalCostCny'] == 2 and first['totalCostUsd'] == 0.5
    replaced = repository.merge_report_second_pass(
        'paper-a', 'en', 'insight',
        {'usage': {'prompt_tokens': 1}, 'costCny': 0, 'costUsd': 0},
        command_id='replace-pass')
    assert replaced['totalCostCny'] == 0 and replaced['totalCostUsd'] == 0
    assert replaced['totalUsage']['prompt_tokens'] == 11
    supervisor.stop()
    supervisor.start()
    stored = repository.get_report('paper-a', 'en')
    assert stored.report == 'unchanged body'
    assert stored.meta == replaced


def test_merge_accumulate_and_replay_preserve_recorded_components(metadata_authority):
    _, repository = metadata_authority
    from lib.storage import StorageError

    assert repository.put_report(PaperReport('paper-a', 'en', 'unchanged body', meta={
        'promptTokens': 10, 'completionTokens': 4, 'costCny': 3, 'costUsd': 0.5,
        'citationAudit': {'suspicious': ['synthetic-id']},
    }), command_id='create-report')
    first = repository.merge_report_second_pass(
        'paper-a', 'en', 'insight',
        {'usage': {'prompt_tokens': 2, 'cache_read_tokens': 1},
         'costCny': 1, 'costUsd': 0.25, 'fired': False}, command_id='merge-pass')
    arguments = {'cost_cny': 2, 'cost_usd': 0.25, 'command_id': 'accumulate-pass'}
    usage = {'prompt_tokens': 5, 'completion_tokens': 3}
    accumulated = repository.accumulate_report_second_pass(
        'paper-a', 'en', 'deepen', usage, **arguments)
    assert accumulated['totalUsage'] == {
        'prompt_tokens': 17, 'completion_tokens': 7,
        'cache_read_tokens': 1, 'cache_write_tokens': 0, 'reasoning_tokens': 0}
    assert accumulated['totalCostCny'] == 6 and accumulated['totalCostUsd'] == 1
    assert accumulated['secondPasses']['insight'] == first['secondPasses']['insight']
    assert accumulated['secondPasses']['deepen']['calls'] == 1
    assert accumulated['citationAudit'] == {'suspicious': ['synthetic-id']}
    assert repository.accumulate_report_second_pass(
        'paper-a', 'en', 'deepen', usage, **arguments) == accumulated
    with pytest.raises(StorageError):
        repository.accumulate_report_second_pass(
            'paper-a', 'en', 'deepen', {'prompt_tokens': 6}, **arguments)
    stored = repository.get_report('paper-a', 'en')
    assert stored.meta == accumulated and stored.report == 'unchanged body'
