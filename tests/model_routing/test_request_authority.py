"""A run's request routing authority is a shared, owner-bound live reference."""
import copy

import pytest

from lib.model_routing import (
    InMemoryModelRoutingRepository,
    ModelRoutingError,
    OwnerBoundary,
    bind_request_routing_authority,
    request_routing_repository,
    share_request_routing_authority,
)

pytestmark = pytest.mark.unit


def test_copies_and_carriers_share_the_one_run_repository():
    repository = InMemoryModelRoutingRepository()
    boundary = OwnerBoundary.create(7, 'workspace')
    root = {'id': 'root'}
    bind_request_routing_authority(root, repository, boundary)

    carrier = {'id': 'proxy'}
    share_request_routing_authority(copy.deepcopy(root), carrier)
    child = {'id': 'child'}
    share_request_routing_authority(copy.copy(carrier), child)

    assert request_routing_repository(child, OwnerBoundary.create(7, 'workspace')) is repository


def test_absent_authority_keeps_the_durable_owner_repository():
    carrier = {'id': 'server-task'}
    share_request_routing_authority({'id': 'parent'}, carrier)
    assert request_routing_repository(carrier, OwnerBoundary.create(7)) is None
    assert carrier == {'id': 'server-task'}


@pytest.mark.parametrize('owner,tenant', [(8, 'workspace'), (7, 'other-workspace'), (7, '')])
def test_another_owner_or_tenant_cannot_mint_from_the_run_authority(owner, tenant):
    root = {'id': 'root'}
    bind_request_routing_authority(
        root, InMemoryModelRoutingRepository(), OwnerBoundary.create(7, 'workspace'))
    with pytest.raises(ModelRoutingError) as refused:
        request_routing_repository(root, OwnerBoundary.create(owner, tenant))
    assert refused.value.kind == 'model_route_unavailable'
