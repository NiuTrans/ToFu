"""CSS negative controls reuse the real resolver without touching source files."""

import builtins
import io
import subprocess

import pytest

from tests.memory import test_memory_modal_specificity as memory
from tests.search import test_recent_search_tofu_specificity as recent

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('negative_control', [
    recent.test_nc_reverting_exclusion_reintroduces_bleed,
    recent.test_nc_unchaining_selector_reintroduces_base_bleed,
    memory.test_nc1_reverting_exclusion_reintroduces_bleed,
], ids=['recent-border', 'recent-padding', 'memory-background'])
def test_css_negative_controls_use_only_in_memory_css(monkeypatch, negative_control):
    css_text = memory._css()
    io_attempts = []

    def reject_io(*args, **kwargs):
        io_attempts.append((args, kwargs))
        raise AssertionError('CSS negative controls must not use file or subprocess I/O')

    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', reject_io)
        patch.setattr(io, 'open', reject_io)
        patch.setattr(subprocess, 'run', reject_io)
        negative_control(css_text)

    assert not io_attempts
