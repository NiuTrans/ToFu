"""An old artifact refuses explicit tree accounting before model/tool effects."""
import pytest
from tests.agent_core.test_native_fee_checkpoint_installed import (
    test_installed_priced_checkpoint_preserves_fees_before_next_http as native_case,
)
pytestmark=pytest.mark.api


def test_installed_tree_checkpoint_refuses_legacy_artifact_before_effects(tmp_path):
    native_case(tmp_path,mode='old_artifact',tree_usage=True)
