"""Ordinary Chat renders sustained-work controls as meaningful operations."""
import pytest

from lib.tasks_pkg.tool_display import tool_round_label

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(('name', 'args', 'expected'), [
    ('get_exploration', {'view': 'catalog'}, 'List exploration plans'),
    ('get_exploration', {'view': 'experiments', 'program_id': 'owned-plan'},
     'Read experiment evidence'),
    ('manage_exploration', {'action': 'create', 'start': True,
                            'request': {'objective': 'Improve the measured baseline'}},
     'Start exploration: Improve the measured baseline'),
    ('manage_exploration', {'action': 'command', 'program_id': 'owned-plan',
                            'command': {'action': 'pause'}}, 'Pause exploration'),
    ('manage_exploration', {'action': 'guide', 'program_id': 'owned-plan'},
     'Guide current experiment'),
])
def test_exploration_operation_is_readable(name, args, expected, caplog):
    assert expected in tool_round_label(name, args)
    assert not [record for record in caplog.records if record.levelname == 'WARNING']
