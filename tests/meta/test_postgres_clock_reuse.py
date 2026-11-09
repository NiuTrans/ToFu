"""Shared compiled bytes must never share a test cluster's clock state."""
import pytest

pytest_plugins = ('tests.support.postgres_cluster', 'tests.support.postgres_worker_clock')
pytestmark = pytest.mark.api


@pytest.fixture(scope='session')
def clock_observations():
    return []


@pytest.mark.parametrize('case', [0, 1])
def test_shared_compilation_keeps_private_server_epochs(
        case, postgres_cluster, postgres_worker_clock_library, clock_observations):
    import psycopg
    from tests.support.postgres_worker_clock import install_clock_function

    install_clock_function(postgres_cluster, postgres_worker_clock_library)
    with psycopg.connect(postgres_cluster) as connection:
        clock = connection.execute('SELECT tofu_worker_lease_clock_v1()').fetchone()[0]
    for previous_library, previous_epoch in clock_observations:
        assert postgres_worker_clock_library == previous_library
        assert clock['epoch'] != previous_epoch
    clock_observations.append((postgres_worker_clock_library, clock['epoch']))
    postgres_cluster.restart()
    with psycopg.connect(postgres_cluster) as connection:
        restarted = connection.execute('SELECT tofu_worker_lease_clock_v1()').fetchone()[0]
    assert restarted['epoch'] != clock['epoch']
