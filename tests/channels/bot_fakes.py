"""Hermetic fakes of the bot platforms (docs/CHANNELS.md C12): 企业微信, QQ, 微信.

Each fake is a real local server (WebSocket or HTTP on 127.0.0.1, port 0) that
speaks the documented wire format, so the production transports run
unchanged. Credentials here are synthetic; nothing reaches a real platform.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from urllib.parse import parse_qs, urlsplit

from websockets.sync.server import serve

WECOM_BOT_ID = 'aibFAKEbot0001'
WECOM_SECRET = 'fake-wecom-secret-0001'  # synthetic, never a real credential
QQ_APP_ID = '102000001'
QQ_SECRET = 'fakeQQsecret0001'  # synthetic
QQ_TOKEN = 'qq-access-token-0001'
WECHAT_TOKEN = 'fake-ilink-bot-token-0001'  # synthetic
OWNER_ID = 'OwnerUser01'
OTHER_ID = 'OtherUser02'


def eventually(check, timeout: float = 5.0):
    """Wait for ``check()`` to be truthy (test-side only); returns its value."""
    deadline = time.monotonic() + timeout
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError('condition not reached in time')
        time.sleep(0.01)


class WsServer:
    """A websockets sync server on an ephemeral port running ``handler(ws)``."""

    def __init__(self, handler):
        self.server = serve(handler, '127.0.0.1', 0)
        self.url = f'ws://127.0.0.1:{self.server.socket.getsockname()[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()


class HttpServer:
    """A threading HTTP server dispatching to ``route(method, path, query, headers, body)``."""

    def __init__(self, route):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get('content-length') or 0)
                body = self.rfile.read(length) if length else b''
                parts = urlsplit(self.path)
                status, payload = route(self.command, parts.path, parse_qs(parts.query),
                                        {k.lower(): v for k, v in self.headers.items()}, body)
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('content-type', 'application/json')
                self.send_header('content-length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        outer.route = route

    def close(self):
        self.server.shutdown()
        self.server.server_close()


# ── 企业微信 智能机器人 long connection ─────────────────────────────────────

class FakeWeCom:
    """aibot_subscribe / ping / aibot_send_msg answered per the 长连接 document."""

    def __init__(self):
        self.frames: list[dict] = []
        self.connections: list = []
        self.subscribe_errcode = 0
        self.send_errcode = 0
        self.drop_on_send = False
        # The n-th send (counted from 1) reaches the platform; the connection closes before its answer.
        self.drop_at_send = 0
        self.server = WsServer(self._handle)
        self.url = self.server.url

    def _handle(self, ws):
        self.connections.append(ws)
        for raw in ws:
            frame = json.loads(raw)
            self.frames.append(frame)
            cmd, req = frame.get('cmd'), frame.get('headers', {}).get('req_id')
            if cmd == 'aibot_subscribe':
                code = self.subscribe_errcode
                ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': code,
                                    'errmsg': 'ok' if code == 0 else 'invalid secret'}))
            elif cmd == 'ping':
                ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': 0, 'errmsg': 'ok'}))
            elif cmd == 'aibot_send_msg':
                if self.drop_on_send or len(self.sent('aibot_send_msg')) == self.drop_at_send:
                    ws.close()
                    return
                ws.send(json.dumps({'headers': {'req_id': req}, 'errcode': self.send_errcode,
                                    'errmsg': 'ok' if self.send_errcode == 0 else 'refused'}))

    def sent(self, cmd: str) -> list[dict]:
        return [frame for frame in self.frames if frame.get('cmd') == cmd]

    def push(self, body: dict, *, cmd: str = 'aibot_msg_callback'):
        eventually(lambda: self.sent('aibot_subscribe'))
        self.connections[-1].send(json.dumps({'cmd': cmd, 'headers': {'req_id': 'cb-' + body.get('msgid', 'x')},
                                              'body': body}))

    def close(self):
        self.server.close()


def wecom_text(msgid: str, text: str, *, sender: str = OWNER_ID, chat: str = '', msgtype: str = 'text',
               **extra) -> dict:
    body = {'msgid': msgid, 'aibotid': WECOM_BOT_ID, 'chattype': 'group' if chat else 'single',
            'from': {'userid': sender}, 'msgtype': msgtype, **extra}
    if chat:
        body['chatid'] = chat
    if msgtype == 'text':
        body['text'] = {'content': text}
    return body


# ── QQ 官方机器人: token endpoint, OpenAPI and WebSocket gateway ─────────────

class FakeQQ:
    def __init__(self):
        self.token_requests: list[dict] = []
        self.api_requests: list[dict] = []
        self.gateway_frames: list[dict] = []
        self.connections: list = []
        self.send_answers: list[tuple[int, dict]] = []  # consumed first, else 200
        self.token_answer: tuple[int, dict] | None = None
        self.seq = 0
        self.ws = WsServer(self._gateway)
        self.http = HttpServer(self._route)
        self.base = self.http.base
        self.token_url = self.base + '/app/getAppAccessToken'

    def _route(self, method, path, query, headers, body):
        if path == '/app/getAppAccessToken':
            self.token_requests.append(json.loads(body or b'{}'))
            if self.token_answer is not None:
                return self.token_answer
            return 200, {'access_token': QQ_TOKEN, 'expires_in': '7200'}
        self.api_requests.append({'method': method, 'path': path, 'auth': headers.get('authorization'),
                                  'body': json.loads(body) if body else None})
        if path == '/gateway':
            return 200, {'url': self.ws.url}
        if self.send_answers:
            return self.send_answers.pop(0)
        return 200, {'id': 'ROBOT1.0_sent', 'timestamp': '2026-11-06T10:00:00+08:00'}

    def _gateway(self, ws):
        self.connections.append(ws)
        ws.send(json.dumps({'op': 10, 'd': {'heartbeat_interval': 45000}}))
        for raw in ws:
            frame = json.loads(raw)
            self.gateway_frames.append(frame)
            if frame.get('op') == 2:
                self.dispatch('READY', {'version': 1, 'session_id': 'sess-1',
                                        'user': {'id': 'bot-1', 'username': 'Tofu Bot', 'bot': True},
                                        'shard': [0, 1]}, ws=ws)
            elif frame.get('op') == 6:
                self.dispatch('RESUMED', '', ws=ws)
            elif frame.get('op') == 1:
                ws.send(json.dumps({'op': 11}))

    def dispatch(self, event: str, data, *, ws=None):
        self.seq += 1
        (ws or self.connections[-1]).send(json.dumps({'op': 0, 's': self.seq, 't': event, 'd': data,
                                                      'id': f'{event}:{self.seq}'}))

    def ops(self, op: int) -> list[dict]:
        return [frame for frame in self.gateway_frames if frame.get('op') == op]

    def sends(self) -> list[dict]:
        return [request for request in self.api_requests if request['path'].startswith('/v2/')]

    def close(self):
        self.ws.close()
        self.http.close()


def qq_c2c(message_id: str, text: str, *, sender: str = OWNER_ID, timestamp='2026-11-06T10:00:00+08:00'):
    return {'id': message_id, 'author': {'id': sender, 'user_openid': sender, 'bot': False},
            'content': text, 'message_type': 0, 'timestamp': timestamp}


def qq_group(message_id: str, text: str, *, group: str = 'GROUPOPENID01', sender: str = OTHER_ID,
             timestamp='2026-11-06T10:00:00+08:00'):
    return {'id': message_id, 'author': {'id': sender, 'member_openid': sender, 'username': 'Alice',
                                         'bot': False},
            'content': text, 'group_openid': group, 'message_type': 0, 'timestamp': timestamp}


# ── 微信 ClawBot (iLink) ─────────────────────────────────────────────────────

class FakeILink:
    def __init__(self):
        self.requests: list[dict] = []
        self.qr_statuses: list[dict] = []
        self.updates: list[dict] = []
        self.sent: list[dict] = []
        self.base = ''
        self.http = HttpServer(self._route)
        self.base = self.http.base
        self.hold = threading.Event()

    def _route(self, method, path, query, headers, body):
        payload = json.loads(body) if body else None
        self.requests.append({'method': method, 'path': path, 'query': query, 'headers': headers,
                              'body': payload})
        if path == '/ilink/bot/get_bot_qrcode':
            return 200, {'qrcode': 'qr-key-1', 'qrcode_img_content': 'https://liteapp.weixin.qq.com/q/fake'}
        if path == '/ilink/bot/get_qrcode_status':
            if self.qr_statuses:
                return 200, self.qr_statuses.pop(0)
            self.hold.wait(0.2)
            return 200, {'status': 'wait'}
        if path == '/ilink/bot/getupdates':
            if self.updates:
                return 200, self.updates.pop(0)
            self.hold.wait(0.2)
            return 200, {'ret': 0, 'msgs': [], 'get_updates_buf': (payload or {}).get('get_updates_buf', '')}
        if path == '/ilink/bot/sendmessage':
            self.sent.append({'headers': headers, 'body': payload})
            return 200, {'ret': 0}
        return 200, {'ret': 0}

    def confirmed(self, **extra) -> dict:
        return {'status': 'confirmed', 'bot_token': WECHAT_TOKEN, 'ilink_bot_id': 'bot@im.bot',
                'baseurl': self.base, 'ilink_user_id': 'owner@im.wechat', **extra}

    def paths(self, path: str) -> list[dict]:
        return [request for request in self.requests if request['path'] == path]

    def close(self):
        self.hold.set()
        self.http.close()


def ilink_message(message_id: int, text: str, *, sender: str = 'owner@im.wechat', message_type: int = 1,
                  context_token: str = 'ctx-1', created_ms: int = 1_793_934_000_000, **extra) -> dict:
    return {'seq': message_id, 'message_id': message_id, 'from_user_id': sender, 'to_user_id': 'bot@im.bot',
            'create_time_ms': created_ms, 'message_type': message_type, 'message_state': 2,
            'context_token': context_token, 'item_list': [{'type': 1, 'text_item': {'text': text}}], **extra}
