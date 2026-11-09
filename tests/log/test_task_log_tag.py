"""Task log tags are whole task ids: log text never shortens an id.

Every durable attempt's task id starts with ``conversation-attempt:``, so the
historical ``task_id[:8]`` tagged ~21k app.log lines ``[Task conversa]`` /
``[rid:conversa]``; the 8-character attempt prefix that replaced it still let
the conversation inspector join other runs' lines. The tag drops only that
constant namespace, so a durable attempt is logged by its whole attempt id and
every other task id is logged whole.
"""
from __future__ import annotations

import pytest

from lib.log import req_id, set_req_id, task_log_tag

pytestmark = pytest.mark.unit

FIRST = '55f61236-291d-41f2-a573-4aed35636e26'
SECOND = 'a88de93e-92eb-40be-8a45-e374f928b2fa'


def test_durable_attempt_ids_are_tagged_by_their_whole_attempt_id():
    assert (task_log_tag('conversation-attempt:' + FIRST),
            task_log_tag('conversation-attempt:' + SECOND)) == (FIRST, SECOND)
    # A swarm child keeps its whole identity under the parent attempt.
    assert (task_log_tag(f'conversation-attempt:{FIRST}#agent:reviewer')
            == f'{FIRST}#agent:reviewer')


@pytest.mark.parametrize('task_id, tag', [
    ('9efe8d2c4b7a41f0', '9efe8d2c4b7a41f0'),   # other ids are logged whole
    ('trace-test-0123456789ab', 'trace-test-0123456789ab'),
    ('conversation-attempt', 'conversation-attempt'),  # only the whole prefix is dropped
    ('?', '?'),
    ('', ''),
    (None, ''),
])
def test_ids_without_the_attempt_prefix_are_logged_whole(task_id, tag):
    assert task_log_tag(task_id) == tag


def test_task_request_id_seed_is_the_whole_attempt_id():
    previous = req_id()
    try:
        set_req_id(task_log_tag('conversation-attempt:' + FIRST))
        assert req_id() == FIRST
    finally:
        set_req_id(previous)
