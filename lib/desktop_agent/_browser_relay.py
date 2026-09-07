"""Browser-assisted transport for SSO-fronted Tofu servers.

Cloud IDEs commonly expose Tofu as ``https://host/proxy/15000/`` behind
their own login wall.  A browser tab can reach that URL because it owns the
gateway cookie; a standalone desktop process cannot, so its otherwise-valid
bridge token is rejected *before the request reaches Tofu*.

This module provides the missing last hop without copying browser cookies or
embedding a browser in the agent:

* the agent opens a loopback-only HTTP broker on one of ``15180..15189``;
* the authenticated Tofu page long-polls that broker for one pending desktop
  poll, forwards it with ``credentials: include``, then returns the response;
* :func:`run_agent` receives a response-shaped object and keeps its existing
  command execution, permissions and result queues unchanged.

The broker accepts browser requests only when their unforgeable ``Origin``
matches the configured Tofu server origin.  It also answers Chromium Private
Network Access preflights.  No endpoint is bound off loopback, no gateway
cookie ever enters this process, and the bridge token is exposed only to the
already-trusted Tofu origin that minted it.

One deliberate exception, the BOOTSTRAP mode: while the agent has no
configured attachment there is no origin to gate on, so ``/v1/status`` and
``/v1/attach`` answer any browser origin.  This is the macOS/Linux
zero-config attach channel — the signed-in Local Control page pushes the
agent its routes + a fresh credential (lib/desktop_agent/_push_attach.py
owns the policy: a page may only attach the agent to the page's own
server).  The moment an attachment exists the strict origin gate closes
over every verb again.
"""

from __future__ import annotations

import json
import queue
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from lib.log import get_logger

logger = get_logger(__name__)

PORT_START = 15180
PORT_END = 15189
_MAX_BODY = 2 * 1024 * 1024
_BROWSER_FRESH_S = 22.0


def origin_of(url: str) -> str:
    """Return the lower-cased HTTP(S) origin of ``url``, else ``''``."""
    try:
        parsed = urlsplit(str(url or '').strip())
    except (TypeError, ValueError) as exc:
        logger.debug('[BrowserRelay] invalid origin %r: %s', url, exc)
        return ''
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
        return ''
    return '%s://%s' % (parsed.scheme.lower(), parsed.netloc.lower())


class RelayResponse:
    """The small subset of ``requests.Response`` consumed by ``run_agent``."""

    def __init__(self, status_code: int, body: str):
        self.status_code = int(status_code)
        self.text = str(body or '')
        self.content = self.text.encode('utf-8', 'replace')

    def json(self):
        return json.loads(self.text)


class _Job:
    def __init__(self, url: str, payload: dict, headers: dict):
        self.id = secrets.token_urlsafe(18)
        self.url = url
        self.payload = payload
        # The page needs only the bridge credential.  Forwarding arbitrary
        # process headers would enlarge this security boundary for no value.
        self.headers = {}
        secret = str((headers or {}).get('X-Bridge-Secret') or '').strip()
        if secret:
            self.headers['X-Bridge-Secret'] = secret
        self.created_at = time.monotonic()
        self.done = threading.Event()
        self.response = None

    def public(self) -> dict:
        return {
            'id': self.id,
            'url': self.url,
            'payload': self.payload,
            'headers': self.headers,
        }


class BrowserRelay:
    """A single-agent, loopback-only browser transport broker."""

    def __init__(self, allowed_urls, log=lambda _msg: None,
                 port_start: int = PORT_START, port_end: int = PORT_END,
                 attach_handler=None, attach_state=None):
        """Create a broker.

        ``allowed_urls`` is a zero-argument callable returning the currently
        configured server URL plus attach candidates.  It is evaluated for
        every browser request so reconnecting the agent updates the origin
        gate without restarting this local service.

        ``attach_handler`` (optional) is ``fn(payload, origin) -> (ok,
        reason, url, transport)`` — the policy entry point for a
        browser-pushed attach (see _push_attach.py); ``attach_state`` is a
        zero-argument callable returning whether an attachment is currently
        configured.  Both must be provided for the bootstrap channel to
        open; without them the broker behaves exactly as before.
        """
        self._allowed_urls = allowed_urls
        self._log = log
        self._port_start = int(port_start)
        self._port_end = int(port_end)
        self._attach_handler = attach_handler
        self._attach_state = attach_state
        self._attach_lock = threading.Lock()
        self._last_attach_at = 0.0
        self._jobs = {}
        self._jobs_lock = threading.Lock()
        self._pending = queue.Queue(maxsize=2)
        self._last_browser_at = 0.0
        self._httpd = None
        self._thread = None
        self.port = 0

    def bootstrap_active(self) -> bool:
        """Whether the unattached bootstrap channel is open RIGHT NOW.

        Fails CLOSED: an unreadable attach state reads as attached, so a
        config hiccup never widens the origin gate.
        """
        if self._attach_handler is None or self._attach_state is None:
            return False
        try:
            return not bool(self._attach_state())
        except Exception as e:
            logger.debug('[AgentRelay] attach-state probe failed: %s', e)
            return False

    def handle_attach(self, payload, origin):
        """Run one pushed-attach attempt — serialized and throttled.

        The handler probes candidate routes (up to 2.5 s each), so a page
        hammering the endpoint would otherwise pile up network waits; one
        attempt per 3 s bounds that while staying invisible to a human
        retrying a stalled install.
        """
        if self._attach_handler is None:
            return False, 'unsupported', '', ''
        with self._attach_lock:
            now = time.monotonic()
            if now - self._last_attach_at < 3.0:
                return False, 'throttled', '', ''
            self._last_attach_at = now
            try:
                return self._attach_handler(payload, origin)
            except Exception as e:
                logger.warning('[AgentRelay] attach handler failed: %s', e)
                return False, 'handler_error', '', ''

    def allowed_origins(self) -> set[str]:
        try:
            urls = self._allowed_urls() or []
        except Exception as e:
            logger.debug('[AgentRelay] allowed URL callback failed: %s', e)
            urls = []
        out = {origin_of(url) for url in urls}
        out.discard('')
        return out

    def origin_allowed(self, origin: str) -> bool:
        return str(origin or '').strip().lower() in self.allowed_origins()

    def note_browser(self) -> None:
        self._last_browser_at = time.monotonic()

    def browser_available(self) -> bool:
        return (self._last_browser_at > 0 and
                time.monotonic() - self._last_browser_at < _BROWSER_FRESH_S)

    def start(self) -> bool:
        """Bind the first free relay port and start serving. Never raises."""
        if self._httpd is not None:
            return True
        relay = self

        class Handler(BaseHTTPRequestHandler):
            server_version = 'TofuAgentRelay/1'

            def log_message(self, _format, *_args):
                return  # never leak bridge-token-bearing request details

            def _cors(self, origin: str) -> None:
                if origin:
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Vary', 'Origin')
                    self.send_header('Access-Control-Allow-Methods',
                                     'GET, POST, OPTIONS')
                    self.send_header('Access-Control-Allow-Headers',
                                     'Content-Type')
                    # Chromium's Private Network Access preflight for an
                    # HTTPS cloud-IDE page reaching http://127.0.0.1.
                    self.send_header('Access-Control-Allow-Private-Network',
                                     'true')
                self.send_header('Cache-Control', 'no-store')

            def _gate(self, bootstrap_ok: bool):
                """The origin check. Returns the origin to CORS-answer with
                ('' for a headerless non-browser caller), or None after
                sending a 403.

                ``bootstrap_ok`` paths (status + attach) open to ANY origin
                while the agent is unattached — there is no configured
                origin to gate on yet, and the page that just served the
                download must be able to find and provision this agent.
                The attach verb's own policy (origin-owns-a-route) is what
                constrains it; every other verb stays on the strict gate.
                """
                origin = (self.headers.get('Origin') or '').strip().lower()
                if origin and relay.origin_allowed(origin):
                    relay.note_browser()
                    return origin
                if bootstrap_ok and relay.bootstrap_active():
                    relay.note_browser()
                    return origin
                self.send_response(403)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return None

            def _json(self, status: int, data: dict, origin: str) -> None:
                raw = json.dumps(data, ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8')
                self.send_response(status)
                self._cors(origin)
                self.send_header('Content-Type',
                                 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _empty(self, status: int, origin: str) -> None:
                self.send_response(status)
                self._cors(origin)
                self.send_header('Content-Length', '0')
                self.end_headers()

            def _read_json(self):
                try:
                    size = int(self.headers.get('Content-Length') or '0')
                except ValueError as exc:
                    logger.debug('[BrowserRelay] invalid Content-Length: %s', exc)
                    return None
                if size <= 0 or size > _MAX_BODY:
                    return None
                try:
                    return json.loads(self.rfile.read(size).decode('utf-8'))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    logger.debug('[BrowserRelay] invalid JSON request body: %s', exc)
                    return None

            def do_OPTIONS(self):  # noqa: N802 - BaseHTTPRequestHandler API
                origin = self._gate(
                    self.path in ('/v1/status', '/v1/attach'))
                if origin is not None:
                    self._empty(204, origin)

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler API
                origin = self._gate(self.path == '/v1/status')
                if origin is None:
                    return
                if self.path == '/v1/status':
                    body = {
                        'kind': 'tofu-agent-browser-relay',
                        'version': 1,
                        'port': relay.port,
                        'pending': relay.pending_count(),
                    }
                    # The page's attach/discovery probe keys on this: an
                    # unattached agent gets its bundle pushed; an attached
                    # one enters the relay flow. Absent when no attach
                    # channel is wired (legacy callers read it as attached).
                    if relay._attach_state is not None:
                        body['attached'] = not relay.bootstrap_active()
                    self._json(200, body, origin)
                    return
                if self.path != '/v1/take':
                    self._empty(404, origin)
                    return
                job = relay.take(timeout=10.0)
                if job is None:
                    self._empty(204, origin)
                else:
                    self._json(200, job, origin)

            def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API
                origin = self._gate(self.path == '/v1/attach')
                if origin is None:
                    return
                if self.path == '/v1/attach':
                    if relay._attach_handler is None:
                        # No push channel wired (source-run agent): the page
                        # reads 404 as 'relay-only build' and stops pushing.
                        self._empty(404, origin)
                        return
                    data = self._read_json()
                    if not isinstance(data, dict):
                        self._empty(400, origin)
                        return
                    ok, reason, url, transport = relay.handle_attach(
                        data, self.headers.get('Origin') or '')
                    status = (200 if ok else
                              409 if reason == 'already_attached' else
                              429 if reason == 'throttled' else 403)
                    self._json(status, {
                        'accepted': bool(ok),
                        'reason': reason,
                        'url': url,
                        'transport': transport,
                    }, origin)
                    return
                if self.path != '/v1/result':
                    self._empty(404, origin)
                    return
                data = self._read_json()
                if not isinstance(data, dict):
                    self._empty(400, origin)
                    return
                ok = relay.complete(
                    str(data.get('id') or ''), data.get('status'),
                    str(data.get('body') or ''))
                self._json(200 if ok else 404, {'accepted': bool(ok)}, origin)

        for port in range(self._port_start, self._port_end + 1):
            try:
                httpd = ThreadingHTTPServer(('127.0.0.1', port), Handler)
            except OSError as exc:
                logger.debug('[BrowserRelay] port %d unavailable: %s', port, exc)
                continue
            httpd.daemon_threads = True
            self._httpd = httpd
            self.port = port
            self._thread = threading.Thread(
                target=httpd.serve_forever, daemon=True,
                name='tofu-agent-browser-relay')
            self._thread.start()
            self._log('Browser relay listening on 127.0.0.1:%d' % port)
            return True
        self._log('Browser relay could not bind ports %d-%d' %
                  (self._port_start, self._port_end))
        logger.warning('[AgentRelay] no free loopback port in %d-%d',
                       self._port_start, self._port_end)
        return False

    def close(self) -> None:
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
                httpd.server_close()
            except OSError as e:
                logger.debug('[AgentRelay] shutdown failed: %s', e)

    def pending_count(self) -> int:
        with self._jobs_lock:
            return len(self._jobs)

    def request(self, url: str, payload: dict, headers: dict,
                timeout: float = 20.0):
        """Ask the authenticated page to perform one desktop poll.

        Returns :class:`RelayResponse`, or ``None`` when the page disappears
        before answering.  The caller then falls back to its ordinary direct
        request and preserves the existing retry/status behaviour.
        """
        if not self.browser_available():
            return None
        job = _Job(url, payload, headers)
        with self._jobs_lock:
            self._jobs[job.id] = job
        try:
            self._pending.put(job.id, timeout=0.5)
        except queue.Full:
            with self._jobs_lock:
                self._jobs.pop(job.id, None)
            return None
        if not job.done.wait(timeout=max(0.1, float(timeout))):
            with self._jobs_lock:
                self._jobs.pop(job.id, None)
            return None
        return job.response

    def take(self, timeout: float = 10.0):
        """Return the next live public job dict, skipping timed-out ids."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                job_id = self._pending.get(timeout=remaining)
            except queue.Empty:
                return None
            with self._jobs_lock:
                job = self._jobs.get(job_id)
            if job is not None:
                return job.public()

    def complete(self, job_id: str, status, body: str) -> bool:
        try:
            code = int(status)
        except (TypeError, ValueError) as exc:
            logger.debug('[BrowserRelay] invalid response status %r: %s',
                         status, exc)
            return False
        if code < 100 or code > 599 or len(body.encode('utf-8')) > _MAX_BODY:
            return False
        with self._jobs_lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            return False
        job.response = RelayResponse(code, body)
        job.done.set()
        return True


__all__ = ['BrowserRelay', 'RelayResponse', 'origin_of',
           'PORT_START', 'PORT_END']
