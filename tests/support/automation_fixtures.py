"""Synthetic automation definitions and producer events shared by domain tests."""

def clock_definition():
    return {
        'schemaVersion': 1, 'id': 'show-start-time', 'name': 'Show task start time',
        'enabled': True,
        'trigger': {'kind': 'event', 'event': 'task.started', 'scope': 'all'},
        'steps': [{'id': 'show', 'action': 'task.show_time',
                   'arguments': {'timezone': 'Asia/Shanghai'}, 'after': []}],
        'policy': {'maxRuntimeSeconds': 10, 'maxModelTokens': 0,
                   'overlap': 'serialize', 'permissionMode': 'auto'},
    }


def skill_definition():
    value = clock_definition()
    value['id'] = 'skills-to-mcp'
    value['trigger'] = {'kind': 'schedule', 'schedule': '0 */6 * * *',
                        'timezone': 'Asia/Shanghai', 'catchUp': 'latest'}
    value['policy']['maxModelTokens'] = 200000
    value['steps'] = [
        {'id': 'check', 'action': 'skills.check', 'after': [], 'arguments': {
            'provider': 'mtskills', 'names': ['citadel', 'hope-skills', 'llm-skills'],
            'initialize': 'baseline'}},
        {'id': 'activate', 'action': 'skills.activate', 'after': ['check'],
         'when': {'source': {'from': 'steps/check', 'pointer': '/changed'},
                  'operator': 'eq', 'value': True},
         'arguments': {'snapshotId': {'$source': {'from': 'steps/check', 'pointer': '/snapshotId'}}}},
        {'id': 'improve', 'action': 'agent.run', 'after': ['activate'],
         'arguments': {'objective': 'Improve the corresponding MCP and verify its contracts',
                       'conversationPolicy': 'new', 'targetMcpIds': ['citadel-mcp'],
                       'skillSnapshotId': {'$source': {'from': 'steps/activate', 'pointer': '/snapshotId'}}}},
        {'id': 'verify', 'action': 'result.verify', 'after': ['improve'],
         'arguments': {'checks': [{'kind': 'mcp_contract', 'id': 'citadel-mcp'}],
                       'resultRef': {'$source': {'from': 'steps/improve', 'pointer': '/resultRef'}}}},
    ]
    return value


def trigger(kind='task.started', event_id='event-1'):
    return {'id': event_id, 'kind': kind, 'occurredAt': 100, 'origin': 'human',
            'evidence': {'startedAt': 100}, 'automationChain': []}
