"""A prior actor-aware SDK refuses new payer transport before model/tool work."""
import os

import pytest

from tests.agent_core.test_native_fee_checkpoint_installed import (
    test_installed_priced_checkpoint_preserves_fees_before_next_http as native_case,
)

pytestmark = pytest.mark.api


def test_actor_payers_refuse_previous_native_archive_before_effects(tmp_path, monkeypatch):
    archive = os.environ.get('TOFU_NATIVE_ACTOR_PAYER_TEST_LEGACY_ARCHIVE')
    if not archive:
        pytest.skip('explicit prior actor-aware native archive required')
    monkeypatch.setenv('TOFU_NATIVE_PRICED_SETTLEMENT_TEST_ARCHIVE', archive)
    native_case(tmp_path, mode='old_artifact', actor_account=True)
