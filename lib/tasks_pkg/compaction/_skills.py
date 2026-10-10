"""Keep loaded workflow instructions in the logical Turn's model context.

The task's committed tool receipts select exact occurrences, never authored
text that resembles a skill. Whole adjacent tool batches remain paired. This
module owns no activation state: completed earlier Turns remain compactable.
"""
from __future__ import annotations


def active_skill_message_ids(messages, task):
    if not isinstance(task, dict):
        return set()
    # The common no-work pass must not traverse Turn-sized receipt metadata.
    if not any(message.get('role') == 'tool'
               and isinstance(message.get('content'), str)
               and message['content'].startswith('Skill loaded:') for message in messages):
        return set()
    latest = {}
    for row in [*(task.get('_checkpointToolRounds') or []), *(task.get('toolRounds') or [])]:
        if row.get('toolName') != 'load_skill':
            continue
        content = row.get('toolContent')
        if not isinstance(content, str) or not content.startswith('Skill loaded:'):
            continue
        skill_id = next((line[4:].strip() for line in content[:1024].splitlines()[:8]
                         if line.startswith('id: ')), '')
        if skill_id and row.get('toolCallId'):
            latest[skill_id] = (row['toolCallId'], content)
    wanted = set(latest.values())
    if not wanted:
        return set()
    protected = set()
    for index, message in enumerate(messages):
        if message.get('role') != 'assistant' or not message.get('tool_calls'):
            continue
        end = index + 1
        while end < len(messages) and messages[end].get('role') == 'tool':
            end += 1
        if any((result.get('tool_call_id'), result.get('content')) in wanted
               for result in messages[index + 1:end]
               if isinstance(result.get('content'), str)):
            protected.update(id(item) for item in messages[index:end])
    return protected
