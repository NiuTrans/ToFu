"""routes/push.py — Unified server-push WebSocket endpoint.

Single global WebSocket per client that multiplexes all real-time events.
Replaces per-task WebSocket, paper report polling, and translation polling.

Protocol:
  Client → Server:
    {action: 'subscribe', channel: 'chat', taskId: '<id>'}
    {action: 'unsubscribe', channel: 'chat', taskId: '<id>'}
    {action: 'abort', channel: 'chat', taskId: '<id>'}

  Server → Client:
    {channel: 'chat', taskId: '<id>', type: 'content_delta', delta: '...'}
    {channel: 'chat', taskId: '<id>', type: 'done', finishReason: 'stop'}
    {channel: 'paper', taskId: '<id>', type: 'progress', ...}
    {channel: 'translate', taskId: '<id>', type: 'done', translated: '...'}
    {channel: 'system', type: 'ping'}
"""

import asyncio
import os

from flask import Blueprint, jsonify, request
from quart import websocket

from lib.log import get_logger
from lib.push import PushClient, hub

logger = get_logger(__name__)

push_bp = Blueprint('push', __name__)


@push_bp.route('/api/push/debug/presence', methods=['POST'])
def debug_presence():
    """Emit synthetic cross-conversation presence frames (DEBUG ONLY).

    Gated OFF by default — only active when ``TOFU_PRESENCE_DEBUG=1``. The
    presence push hub is an in-process singleton, so a standalone script
    cannot light up a live browser by itself; this endpoint runs the registry
    mutations INSIDE the server process so their broadcasts reach connected
    WebSocket clients. Used by ``debug/presence_smoke.py --live`` to eyeball
    the "who's working" strip without orchestrating two real conversations.

    Body: ``{root: "<abs path>", action: "scenario"|"subagents"|"clear"}``.
    ``scenario`` announces two sibling-CONVERSATION peers touching a shared file
    (cross-conversation conflict); ``subagents`` announces ONE conversation with
    two SUB-AGENTS clobbering the same file (within-conversation conflict +
    nested rows); both leave the peers ACTIVE so the strip stays lit. ``clear``
    departs everything.
    """
    if os.environ.get('TOFU_PRESENCE_DEBUG') not in ('1', 'true', 'yes'):
        return jsonify({'ok': False, 'error': 'presence debug disabled '
                        '(set TOFU_PRESENCE_DEBUG=1 to enable)'}), 403
    body = request.get_json(silent=True) or {}
    root = (body.get('root') or '').strip()
    action = (body.get('action') or 'scenario').strip()
    if not root:
        return jsonify({'ok': False, 'error': 'root is required'}), 400
    from lib import presence
    if action == 'clear':
        presence.depart(root, 'dbg-peer-1')
        presence.depart(root, 'dbg-peer-2')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-1')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-2')
        presence.depart(root, 'dbg-swarm')
        return jsonify({'ok': True, 'action': 'clear', 'root': root})
    if action == 'subagents':
        # ONE conversation, TWO sub-agents clobbering the SAME file → a
        # within-conversation conflict advisory + nested rows on the strip.
        presence.announce(root, 'dbg-swarm', task_id='dbg-task-3',
                          title='Swarm session', objective='parallel refactor',
                          phase='working')
        for aid in ('agent-coder-1', 'agent-coder-2'):
            presence.announce(root, 'dbg-swarm', agent_id=aid, task_id='dbg-task-3',
                              title='coder', parent_title='Swarm session',
                              phase='working')
            presence.record_files(root, 'dbg-swarm',
                                  [{'path': 'lib/llm/stream.py', 'action': 'patched'}],
                                  agent_id=aid)
        snap = presence.snapshot(root)
        logger.info('[Push] debug presence SUB-AGENT scenario fired root=%s peers=%d',
                    root, len(snap.get('peers') or []))
        return jsonify({'ok': True, 'action': 'subagents', 'root': root,
                        'activePeers': len(snap.get('peers') or [])})
    # scenario: two peers, a shared-file conflict, both left active.
    presence.announce(root, 'dbg-peer-1', task_id='dbg-task-1',
                      title='Refactor the parser', objective='make it ship',
                      phase='working')
    presence.announce(root, 'dbg-peer-2', task_id='dbg-task-2',
                      title='Tune the LLM stream', objective='cut TTFT',
                      phase='working')
    presence.record_files(root, 'dbg-peer-1',
                          [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    presence.record_files(root, 'dbg-peer-2',
                          [{'path': 'lib/llm/stream.py', 'action': 'patched'}])
    snap = presence.snapshot(root)
    logger.info('[Push] debug presence scenario fired root=%s peers=%d',
                root, len(snap.get('peers') or []))
    return jsonify({'ok': True, 'action': 'scenario', 'root': root,
                    'activePeers': len(snap.get('peers') or [])})


@push_bp.websocket('/api/push')
async def push_ws():
    """Global push channel WebSocket endpoint."""
    client = PushClient()
    hub.register(client)
    logger.info('[Push] WS connected (clients=%d)', hub.client_count)

    send_task = None
    recv_task = None

    async def _sender():
        """Drain the client queue and send frames to the WebSocket."""
        try:
            while True:
                frame = await client.drain()
                if frame is None:
                    break
                await websocket.send_json(frame)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug('[Push] Sender error: %s', e)

    async def _receiver():
        """Receive client commands (subscribe, unsubscribe, abort)."""
        try:
            while True:
                raw = await websocket.receive_json()
                if not raw or not isinstance(raw, dict):
                    continue
                action = raw.get('action', '')
                channel = raw.get('channel', '')
                task_id = raw.get('taskId', '*')

                if action == 'subscribe' and channel:
                    hub.subscribe(client, channel, task_id)
                    logger.debug('[Push] Subscribe: channel=%s taskId=%s', channel, task_id[:8])
                elif action == 'unsubscribe' and channel:
                    hub.unsubscribe(client, channel, task_id)
                elif action == 'abort' and channel == 'chat' and task_id != '*':
                    _handle_abort(task_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug('[Push] Receiver error: %s', e)

    try:
        send_task = asyncio.create_task(_sender())
        recv_task = asyncio.create_task(_receiver())
        await asyncio.gather(send_task, recv_task)
    except asyncio.CancelledError:
        pass
    finally:
        client.disconnect()
        hub.unregister(client)
        if send_task and not send_task.done():
            send_task.cancel()
        if recv_task and not recv_task.done():
            recv_task.cancel()
        logger.info('[Push] WS disconnected (clients=%d)', hub.client_count)


def _handle_abort(task_id: str):
    """Handle a client abort request for a chat task.

    Chat tasks predate the unified ``TaskRuntime.abort_event`` flag and
    still gate their work loop on ``task['aborted']``. We set both so
    the orchestrator stops regardless of which path it consults.
    """
    from lib.tasks_pkg import tasks, tasks_lock
    with tasks_lock:
        task = tasks.get(task_id)
    if not task:
        return
    task['aborted'] = True
    abort_evt = task.get('abort_event')
    if abort_evt is not None:
        try:
            abort_evt.set()
        except Exception as e:
            logger.debug('[Push] abort_event.set failed: %s', e)
    logger.info('[Push] Client abort for task %s', task_id[:8])
