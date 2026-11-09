"""Seed and read a restart's durable state beside a real resident storage host.

Used only by ``tests/server/test_restart_resume_application.py``. The helper
attaches to the resident storage service the test started (the
``TOFU_STORAGE_CONNECTION_FILE`` of its environment), exactly as the execution
host does, and plays the stopping server generation: a Global Router that
asked for the restart (its turn ended), the gate's accepted restart with its
open notice, and a chat whose running Turn the shutdown quiesce recorded and
settled. ``read`` reports what the next generation did. No model runs here.
"""
from copy import deepcopy
import json
import sys

OWNER = 1
HUMAN_UI = {'kind': 'human-ui', 'ownerUserId': OWNER, 'subject': 'session-key', 'credential': 'session',
            'browser': 'fetch-metadata', 'userAgent': 'Mozilla/5.0', 'remoteAddr': '127.0.0.1'}


def _running(conversation: str, content: str) -> dict:
    from lib import turn_lifecycle as turns
    from lib.conversation_sync.execution_identity import capture_execution_identity
    from lib.identity import PrincipalContext
    principal = PrincipalContext.user(subject_id='owner', owner_user_id=OWNER)
    config = capture_execution_identity({'model': 'probe-model'}, OWNER, principal)
    pair = turns.create_turn_pair(conversation, command_id=conversation + ':input',
        input_projection={'content': content}, config=deepcopy(config), user_id=OWNER,
        conversation_defaults={'allowCreate': True, 'title': conversation, 'settings': {}})
    task = {'id': conversation + '-task', 'convId': conversation, '_turnId': pair['turn']['turnId'],
            '_attemptId': pair['attempt']['attemptId'], '_userId': OWNER, '_attended': True,
            '_principalContext': principal.to_payload(), 'config': deepcopy(config),
            'status': 'running', 'model': 'probe-model', 'content': 'Half of the answer', 'thinking': '',
            'toolRounds': []}
    turns.bind_task(task['_attemptId'], task['id'], user_id=OWNER)
    turns.record_task_event(task, {'type': 'delta', 'content': task['content']})
    return task


def seed() -> dict:
    from lib import boot_identity
    from lib import lifecycle_approval as gate
    from lib import turn_lifecycle as turns
    from lib.conversations.restart_resume import record_quiesced_conversations
    router = _running('router', 'Restart Tofu for me.')
    router.update(status='done', finishReason='stop')
    turns.record_task_event(router, {'type': 'done', 'finishReason': 'stop'})
    chat = _running('chat', 'Write the long report.')
    grantee = {'kind': 'global_router', 'conversationId': 'router', 'bindingRevision': 1, 'tenant': None}
    gate.set_delegation(OWNER, True, grantee=grantee, provenance=HUMAN_UI)
    requester = {**grantee, 'turnId': router['_turnId'], 'attemptId': router['_attemptId']}
    action, _ = gate.begin_delegated_action(
        OWNER, requester, scope='restart', key='probe-restart', reason='The owner asked for it',
        trigger={'kind': 'owner_message', 'turnId': 'owner-turn', 'text': 'Restart Tofu for me.'},
        target={'kind': 'server'})
    gate.accept_delegated_restart(OWNER, action['id'], requester, interrupted=['chat'],
                                  old_code=boot_identity.code_fingerprint(),
                                  preparation={'ok': True, 'phases': ['checking', 'cached'], 'seconds': 1})
    # The stopping generation's shutdown quiesce: record, then settle as paused.
    record_quiesced_conversations([chat])
    chat.update(status='aborted', aborted=True, _abort_reason='server_shutdown')
    turns.record_task_event(chat, {'type': 'aborted'})
    return {'actionId': action['id'], 'chatTurn': chat['_turnId'], 'chatAttempt': chat['_attemptId']}


def read() -> dict:
    from lib import lifecycle_approval as gate
    from lib import turn_lifecycle as turns
    from lib.turn_initiation import resolve_turn_initiator
    chat = turns.list_turns('chat', user_id=OWNER, lane_id='main')['turns'][-1]
    attempt = turns.get_attempt(chat['currentAttemptId'], user_id=OWNER)
    inputs = [turn for turn in turns.list_turns('router', user_id=OWNER, lane_id='main')['turns']
              if turn.get('actor') == 'human']
    [action] = gate.delegated_actions(OWNER)
    [notice] = gate.restart_notices(states=('pending', 'delivering', 'delivered', 'failed', 'undeliverable'))
    return {'chatTurn': chat['turnId'], 'chatAttemptCommand': attempt.get('commandId'),
            'chatAttemptOperation': attempt.get('operation'),
            'routerInputs': [{'content': (turn.get('projection') or {}).get('content', ''),
                              'initiator': resolve_turn_initiator(turn)} for turn in inputs],
            'actionStatus': action['status'], 'notice': {key: notice.get(key) for key in (
                'state', 'disposition', 'conversationId', 'commandId', 'text')}}


def main(mode: str) -> int:
    from lib.storage import start_storage
    start_storage()
    print('PROBE ' + json.dumps(seed() if mode == 'seed' else read()), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))
