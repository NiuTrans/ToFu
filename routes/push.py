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
import hashlib
import hmac
import os

from quart import Blueprint, websocket

from lib.quart_sync import request_json

from lib.api_response import api_error, api_ok
from lib.log import get_logger, resolve_inbound_rid
from lib.push import PushClient, hub

logger = get_logger(__name__)

push_bp = Blueprint('push', __name__)


async def _join_socket_halves(*tasks):
    """End a duplex socket as soon as either transport half terminates."""
    pending = set(tasks)
    try:
        _done, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in pending:
            if not task.done():
                task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def _push_ws_peer():
    """Return the direct ASGI socket peer, never a forwarded header."""
    try:
        peer = (websocket.scope or {}).get('client')
        if peer:
            return peer
    except Exception as e:
        logger.debug('[Push] WS peer scope read failed: %s', e)
    try:
        return websocket.headers.get('Remote-Addr') or ''
    except Exception as e:
        logger.debug('[Push] WS Remote-Addr fallback unavailable: %s', e)
        return ''


def _push_ws_token(headers, cookies) -> str:
    """Extract a WebSocket credential in the HTTP gate's priority order."""
    auth = (headers.get('Authorization') or '') if headers is not None else ''
    parts = auth.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == 'bearer':
        token = parts[1].strip()
        if token:
            return token
    x_api_key = ((headers.get('x-api-key') or '').strip()
                 if headers is not None else '')
    if x_api_key.startswith(('tofu_live_', 'tofu_admin_')):
        return x_api_key
    from routes.api_v1.auth import SESSION_COOKIE
    cookie_token = ((cookies.get(SESSION_COOKIE) or '').strip()
                    if cookies is not None else '')
    if cookie_token.startswith(('tofu_live_', 'tofu_admin_')):
        return cookie_token
    return ''


def _legacy_ws_tunnel_ok(headers, cookies) -> bool:
    """WebSocket twin of the HTTP gate's deprecated TUNNEL_TOKEN shim."""
    expected_secret = os.environ.get('TUNNEL_TOKEN', '')
    if not expected_secret:
        return False
    supplied = ((headers.get('X-Tunnel-Token') or '')
                if headers is not None else '')
    if supplied and hmac.compare_digest(supplied, expected_secret):
        return True
    cookie_value = ((cookies.get('_tunnel_auth') or '')
                    if cookies is not None else '')
    expected_cookie = hashlib.sha256(expected_secret.encode()).hexdigest()[:32]
    return bool(cookie_value and hmac.compare_digest(cookie_value, expected_cookie))


def _resolve_push_ws_auth(headers, cookies, peer):
    """Resolve or reject a push socket using the HTTP gate's policy.

    Returns ``(AuthContext | None, reason)``.  This is deliberately a pure
    seam over supplied transport values so the otherwise long-lived socket
    handler can be tested without accepting a connection first.
    """
    from lib.api_keys import AuthContext, local_admin_context, touch_key, validate_token
    from lib.auth_mode import requires_credential
    from routes.api_v1.auth import open_mode_peer_allowed

    token = _push_ws_token(headers, cookies)
    invalid_token = False
    if token:
        try:
            ctx = validate_token(token)
        except Exception as e:
            logger.warning('[Push] WS token validation failed closed: %s', e)
            return None, 'auth_error'
        if ctx is not None:
            try:
                touch_key(ctx.key_id)
            except Exception as e:
                logger.debug('[Push] WS key touch failed: %s', e)
            return ctx, 'token'
        # Match HTTP: a supplied invalid token is definitive in a mode that
        # requires credentials. In local open mode it may still fall back to
        # the synthetic principal, preserving the zero-setup browser flow.
        if requires_credential():
            return None, 'invalid_token'
        invalid_token = True

    if invalid_token:
        if open_mode_peer_allowed(peer):
            return local_admin_context(), 'open_local'
        return None, 'invalid_token'

    if _legacy_ws_tunnel_ok(headers, cookies):
        return AuthContext(
            key_id='', name='tunnel', scopes=frozenset({'admin'}),
            rate_limit_rpm=0, rate_limit_tpd=0, via_tunnel_token=True,
        ), 'tunnel'

    if not requires_credential() and open_mode_peer_allowed(peer):
        return local_admin_context(), 'open_local'
    return None, 'credential_required'


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
        return api_error('presence debug disabled '
                        '(set TOFU_PRESENCE_DEBUG=1 to enable)', status=403)
    body = request_json(silent=True) or {}
    root = (body.get('root') or '').strip()
    action = (body.get('action') or 'scenario').strip()
    if not root:
        return api_error('root is required', status=400)
    from lib import presence
    if action == 'clear':
        presence.depart(root, 'dbg-peer-1')
        presence.depart(root, 'dbg-peer-2')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-1')
        presence.depart(root, 'dbg-swarm', agent_id='agent-coder-2')
        presence.depart(root, 'dbg-swarm')
        return api_ok({'action': 'clear', 'root': root})
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
        return api_ok({'action': 'subagents', 'root': root,
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
    return api_ok({'action': 'scenario', 'root': root,
                   'activePeers': len(snap.get('peers') or [])})


@push_bp.websocket('/api/push')
async def push_ws():
    """Global push channel WebSocket endpoint.

    Resolves ``AuthContext`` at handshake and stashes ``user_id`` on the
    ``PushClient`` so downstream frame handlers scope by owner without
    re-doing auth. Quart's ``before_request`` middleware does NOT fire
    on WS routes, so we resolve inline from ``websocket.cookies`` and
    ``websocket.headers`` — same transports the HTTP gate accepts, but
    read from the WebSocket-scoped globals rather than ``request``.

    Open-mode loopback clients retain the zero-setup personal-install path;
    remote open-mode clients and every private/multi-user client must satisfy
    the same credential policy as HTTP.  A valid principal's ``user_id`` is
    stashed so snapshots and control frames remain owner-scoped.
    """
    # ── Correlation id (pt_3d28727f / pt_ccaec091) ────────────────
    # Quart's @app.before_request does NOT run on WS routes, so the HTTP
    # middleware that resolves X-Request-ID never fires here and this socket
    # would otherwise be invisible in the request-id log axis. A browser
    # WebSocket cannot set custom headers, so the client puts its id in the
    # `_rid` QUERY PARAM (static/js/push.js::_buildUrl); we honor it through
    # the SAME validated resolver the HTTP path uses (lib.log), so one id
    # space covers both transports and a malformed id can never reach a log
    # line.
    #
    # A socket ALWAYS has an id: resolve_inbound_rid mints one when the
    # client's is absent or unsafe, and the except branch below mints one
    # too. There is deliberately no "no id" state — a socket whose lines
    # cannot be joined to anything is the exact failure this closes.
    #
    # Keep the rid on PushClient and pass it explicitly to socket log calls.
    # lib.log now uses a ContextVar, but avoiding ambient state here also keeps
    # frame handlers deterministic when they run in separately-created tasks.
    try:
        _rid = resolve_inbound_rid(websocket.headers.get('X-Request-ID'),
                                   websocket.args.get('_rid'))
    except Exception as _e:
        # Never let correlation bookkeeping break the socket — but never
        # leave it without an id either.
        _rid = resolve_inbound_rid(None, None)
        logger.debug('[Push] rid resolve failed (minted %s): %s', _rid, _e)

    # ── Resolve WS handshake auth (HTTP before_request does not run) ────
    try:
        _ctx, _auth_reason = _resolve_push_ws_auth(
            websocket.headers, websocket.cookies, _push_ws_peer())
    except Exception as _e:
        logger.warning('[Push] WS auth resolve failed closed (rid=%s): %s',
                       _rid, _e)
        _ctx, _auth_reason = None, 'auth_error'
    if _ctx is None:
        # Abort before the first send/receive accepts the upgrade, allowing
        # Quart/Hypercorn to return an HTTP 401 handshake response.
        from quart import abort
        logger.warning('[Push] WS rejected (reason=%s, rid=%s)',
                       _auth_reason, _rid)
        abort(401)
    _user_id = getattr(_ctx, 'user_id', '') or ''

    client = PushClient(user_id=_user_id, req_id=_rid)
    hub.register(client)
    from lib.observability import connection_close, connection_open
    connection_open('ws', 'push')
    logger.info('[Push] WS connected (clients=%d, user=%s, rid=%s)',
                hub.client_count, _user_id or '<unscoped>', _rid)

    send_task = None
    recv_task = None

    async def _sender():
        """Drain the client queue and send frames to the WebSocket."""
        try:
            while True:
                frame = await client.drain()
                if frame is None:
                    break
                # A ping means the 30s drain window elapsed with no traffic —
                # use it as the subscription-registry heartbeat so a LIVING
                # subscriber's cross-replica lease (sub:*) never expires under
                # the 90s TTL (design B.5.2, refresh at ~ttl/3).
                if frame.get('type') == 'ping':
                    try:
                        hub.refresh_subscriptions()
                    except Exception as e:
                        logger.debug('[Push] registry heartbeat failed: %s', e)
                await websocket.send_json(frame)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug('[Push] Sender error (rid=%s): %s', _rid, e)

    async def _receiver():
        """Receive client commands (subscribe, unsubscribe, abort, ping)."""
        try:
            while True:
                raw = await websocket.receive_json()
                _handle_client_frame(client, raw)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug('[Push] Receiver error (rid=%s): %s', _rid, e)

    try:
        send_task = asyncio.create_task(_sender())
        recv_task = asyncio.create_task(_receiver())
        # Either half ending means the socket lifetime is over. Waiting for
        # gather() to see BOTH finish stranded a receiver after a send error,
        # or a sender until its next 30s keepalive after peer disconnect.
        # Cancel and drain the sibling immediately so hub membership and its
        # queued frames are released at the transport boundary.
        await _join_socket_halves(send_task, recv_task)
    except asyncio.CancelledError:
        pass
    finally:
        client.disconnect()
        hub.unregister(client)
        if send_task and not send_task.done():
            send_task.cancel()
        if recv_task and not recv_task.done():
            recv_task.cancel()
        logger.info('[Push] WS disconnected (clients=%d, rid=%s)',
                    hub.client_count, _rid)
        connection_close('ws', 'push', 'disconnected')


def _handle_client_frame(client: PushClient, raw) -> None:
    """Dispatch one inbound client command frame.

    Extracted from the ``_receiver`` coroutine so the routing — in particular
    the latency ``ping`` → ``pong`` echo — is directly unit-testable without a
    live WebSocket. Never writes the socket directly: outbound frames are
    ENQUEUED onto the client's queue so ``_sender`` stays the sole writer of
    the ASGI WebSocket (two coroutines writing it concurrently can
    interleave/corrupt frames).
    """
    if not raw or not isinstance(raw, dict):
        return
    action = raw.get('action', '')
    channel = raw.get('channel', '')
    task_id = raw.get('taskId', '*')

    if action == 'subscribe' and channel:
        hub.subscribe(client, channel, task_id)
        logger.debug('[Push] Subscribe: channel=%s taskId=%s rid=%s',
                     channel, task_id[:8], client.req_id)
        # ── pt_conv_state_ssot P1.5: server-authoritative connect snapshot ──
        # When a client subscribes to the notify wildcard (the sidebar's
        # subscription) send it a one-shot snapshot of the current running-
        # task state so it has authoritative busy info without waiting for
        # the next notify_conv_changed frame or the 25/90s poll fallback.
        # Delivered DIRECTLY to this client's outbound queue — never via
        # hub.push_event, which would fan out to every subscriber (leaking
        # snapshots into unrelated tabs/users) and cross-replica bus.
        if channel == 'notify' and task_id == '*':
            try:
                from lib.agent_core.push import build_conv_state_snapshot
                # pt_ab42421158214591: use the user_id resolved at
                # handshake and stashed on the client. Empty string is
                # the pre-auth default → unscoped snapshot (same as
                # before this change for personal-install / open-mode).
                # A real AuthContext.user_id gives a per-user scoped
                # snapshot that cannot leak sibling tenants' tasks.
                client.enqueue(build_conv_state_snapshot(user_id=client.user_id))
            except Exception as e:
                logger.debug('[Push] connect snapshot enqueue failed '
                             '(rid=%s): %s', client.req_id, e)
    elif action == 'unsubscribe' and channel:
        hub.unsubscribe(client, channel, task_id)
    elif action == 'abort' and channel == 'chat' and task_id != '*':
        _handle_abort(task_id, req_id=client.req_id,
                      user_id=client.user_id)
    elif action == 'ping':
        # Round-trip latency probe. Echo the client's timestamp back so the
        # client can compute RTT = now - t. Pure echo (no shared state) → works
        # on whatever replica the socket landed on. Route it through the
        # client's CONTROL LANE (PushClient.enqueue_control), which the single
        # _sender drains BEFORE the data backlog: _sender stays the sole
        # writer of the ASGI socket (two coroutines writing it concurrently
        # can interleave/corrupt frames), but a liveness answer must never
        # queue behind MBs of event frames — under loop congestion that delay
        # outlives the client's ping watchdog and it force-closes a HEALTHY
        # socket (pt_afbaf3d7).
        client.enqueue_control({'channel': 'system', 'type': 'pong', 't': raw.get('t')})


def _handle_abort(task_id: str, req_id: str = '', user_id: str = ''):
    """Handle a client abort request for a chat task.

    Chat tasks predate the unified ``TaskRuntime.abort_event`` flag and
    still gate their work loop on ``task['aborted']``. We set both so
    the orchestrator stops regardless of which path it consults.

    ``req_id`` is the requesting socket's correlation id, passed in because
    this is a module-level function with no view of the handler coroutine's
    locals. "I pressed stop and it kept going" is the most-investigated
    complaint on this channel, so this line has to name the socket that
    asked — otherwise the user's id gets them only the connect/disconnect
    pair and nothing in between.
    """
    from lib.tasks_pkg import tasks, tasks_lock
    with tasks_lock:
        task = tasks.get(task_id)
        if not task:
            outcome = 'missing'
        elif user_id and str(task.get('_userId') or '') != str(user_id):
            outcome = 'forbidden'
        else:
            # Mutate under the same registry lock as the ownership read so a
            # concurrent discard/replacement cannot turn the check into a
            # time-of-check/time-of-use authorization race.
            task['aborted'] = True
            abort_evt = task.get('abort_event')
            outcome = 'aborted'
    if outcome == 'missing':
        logger.info('[Push] Client abort for unknown task %s (rid=%s)',
                    task_id[:8], req_id)
        return
    if outcome == 'forbidden':
        logger.warning('[Push] Client abort refused for foreign task %s '
                       '(user=%s, rid=%s)', task_id[:8], user_id, req_id)
        return
    if abort_evt is not None:
        try:
            abort_evt.set()
        except Exception as e:
            logger.debug('[Push] abort_event.set failed (rid=%s): %s',
                         req_id, e)
    logger.info('[Push] Client abort for task %s (rid=%s)',
                task_id[:8], req_id)
