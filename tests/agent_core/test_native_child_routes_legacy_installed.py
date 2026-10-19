"""Previous tree-aware artifacts refuse child-route snapshots before effects."""
import pytest
from tests.agent_core.test_native_fee_checkpoint_installed import (
    test_installed_priced_checkpoint_preserves_fees_before_next_http as native_case,
)

pytestmark = pytest.mark.api


def test_child_routes_refuse_previous_tree_archive_before_effects(tmp_path):
    native_case(tmp_path,mode='old_artifact',tree_usage=True,child_routes=True)
