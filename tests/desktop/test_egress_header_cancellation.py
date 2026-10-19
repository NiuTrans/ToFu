"""Cancellation must escape header waits without replaying relay requests."""
from unittest.mock import patch

import pytest

from lib.desktop import egress
from lib.llm.llm_errors import AbortedError

pytestmark = pytest.mark.unit


def test_cancel_before_enqueue():
    with patch('lib.desktop.enqueue_desktop_command') as enqueue:
        with pytest.raises(AbortedError):
            egress.open_stream('https://api.anthropic.com/v1/messages',
                               agent_id='agent-1', user_id='1', abort_check=lambda: True)
    enqueue.assert_not_called()


def test_cancel_during_header_wait_closes_without_failover():
    checks = iter([False, False, True])
    with patch('lib.desktop.egress.route_candidates', return_value=['a1', 'a2']), \
         patch('lib.desktop.enqueue_desktop_command', return_value=('cmd', None)) as enqueue, \
         patch('lib.desktop.get_frames', return_value=([], False)), \
         patch('lib.desktop.egress.cancel_stream') as cancel, \
         patch('lib.desktop.egress.time.sleep'):
        with pytest.raises(AbortedError):
            egress.open_stream('https://api.anthropic.com/v1/messages', user_id='1',
                               abort_check=lambda: next(checks))
    assert enqueue.call_count == 1
    assert cancel.call_count == 1
    assert cancel.call_args.args[1:] == ('a1', '1')
