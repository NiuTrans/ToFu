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

from flask import Blueprint
from quart import websocket

from lib.log import get_logger
from lib.push import PushClient, hub

logger = get_logger(__name__)

push_bp = Blueprint('push', __name__)


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
