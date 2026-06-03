"""tests/test_trajectory.py — Trajectory flatten across all 4 formats."""

import unittest


def _sample_task(*, with_tools=True, multimodal=False):
    """Build a finished-task fixture for flatten()."""
    user_content = (
        [{'type': 'text', 'text': 'Refactor the foo module'}]
        if multimodal else 'Refactor the foo module'
    )
    task = {
        'id': 'task_abc123',
        'kind': 'chat',
        'status': 'done',
        'finishReason': 'stop',
        'content': 'Done — refactored 3 functions.',
        'thinking': 'Let me look at foo.py first...',
        'usage': {'input_tokens': 100, 'output_tokens': 50,
                   'total_tokens': 150},
        'messages': [
            {'role': 'system', 'content': 'You are helpful.'},
            {'role': 'user', 'content': user_content},
        ],
        'events': [
            {'type': 'phase', 'phase': 'thinking'},
            {'type': 'delta', 'content': 'Done'},
            {'type': 'done', 'finishReason': 'stop'},
        ],
        'toolRounds': [],
    }
    if with_tools:
        task['toolRounds'] = [{
            'tool_calls': [{
                'id': 'call_1',
                'type': 'function',
                'function': {
                    'name': 'read_files',
                    'arguments': '{"path": "foo.py"}',
                },
            }],
            'results': [{
                'tool_call_id': 'call_1',
                'name': 'read_files',
                'result': '<contents of foo.py>',
            }],
        }]
    return task


class TrajectoryFormatTest(unittest.TestCase):

    def test_format_registry(self):
        from lib.trajectory import AVAILABLE_FORMATS
        self.assertEqual(set(AVAILABLE_FORMATS),
                          {'sharegpt', 'openai-finetune', 'anthropic',
                           'tofu-native'})

    def test_unknown_format_raises(self):
        from lib.trajectory import flatten
        with self.assertRaises(ValueError):
            flatten(_sample_task(), 'totally-unknown-format')

    def test_sharegpt_basic(self):
        from lib.trajectory import flatten
        out = flatten(_sample_task(with_tools=False), 'sharegpt')
        self.assertEqual(out['format'], 'sharegpt')
        traj = out['trajectory']
        self.assertIsInstance(traj, list)
        # system, user, assistant
        roles = [r['from'] for r in traj]
        self.assertIn('system', roles)
        self.assertIn('human', roles)
        self.assertIn('gpt', roles)
        # last entry is the assistant
        self.assertEqual(traj[-1]['from'], 'gpt')
        self.assertEqual(traj[-1]['value'], 'Done — refactored 3 functions.')

    def test_sharegpt_with_tools_inlines_tool_calls(self):
        from lib.trajectory import flatten
        out = flatten(_sample_task(with_tools=True), 'sharegpt')
        traj = out['trajectory']
        # tool message present
        self.assertTrue(any(r['from'] == 'tool' for r in traj))
        # final assistant carries serialised tool_calls
        last = traj[-1]
        self.assertEqual(last['from'], 'gpt')
        self.assertIn('tool_calls', last)

    def test_openai_finetune_shape(self):
        from lib.trajectory import flatten
        out = flatten(_sample_task(with_tools=True), 'openai-finetune')
        traj = out['trajectory']
        self.assertIn('messages', traj)
        msgs = traj['messages']
        roles = [m['role'] for m in msgs]
        self.assertEqual(roles[0], 'system')
        self.assertEqual(roles[1], 'user')
        self.assertIn('tool', roles)
        self.assertEqual(roles[-1], 'assistant')
        # final assistant has tool_calls
        self.assertIn('tool_calls', msgs[-1])

    def test_anthropic_shape(self):
        from lib.trajectory import flatten
        out = flatten(_sample_task(with_tools=True), 'anthropic')
        traj = out['trajectory']
        self.assertEqual(traj.get('system'), 'You are helpful.')
        self.assertIn('messages', traj)
        # Last message is assistant with content blocks
        last = traj['messages'][-1]
        self.assertEqual(last['role'], 'assistant')
        block_types = [b['type'] for b in last['content']]
        self.assertIn('thinking', block_types)
        self.assertIn('text', block_types)
        self.assertIn('tool_use', block_types)
        # tool_use carries parsed input dict
        tool_use = next(b for b in last['content'] if b['type'] == 'tool_use')
        self.assertEqual(tool_use['name'], 'read_files')
        self.assertEqual(tool_use['input'], {'path': 'foo.py'})

    def test_tofu_native_lossless(self):
        from lib.trajectory import flatten
        task = _sample_task(with_tools=True)
        out = flatten(task, 'tofu-native')
        traj = out['trajectory']
        self.assertEqual(traj['task_id'], 'task_abc123')
        self.assertEqual(traj['status'], 'done')
        self.assertEqual(len(traj['events']), 3)
        self.assertEqual(len(traj['tool_rounds']), 1)
        self.assertEqual(traj['final_assistant']['content'],
                         'Done — refactored 3 functions.')

    def test_multimodal_user_collapses_text(self):
        from lib.trajectory import flatten
        out = flatten(_sample_task(multimodal=True, with_tools=False),
                       'openai-finetune')
        msgs = out['trajectory']['messages']
        user = next(m for m in msgs if m['role'] == 'user')
        self.assertEqual(user['content'], 'Refactor the foo module')


if __name__ == '__main__':
    unittest.main()
