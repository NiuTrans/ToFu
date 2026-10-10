"""Unified high-level page API over the versioned Browser Bridge."""

from __future__ import annotations

import time
from typing import Any

from .access import normalize_domain, require_access
from .protocol import BrowserCapability, require_capabilities
from .sessions import (
    BrowserSessionLease, bind_lease_tab, release_browser_lease,
)


class BrowserCommandError(RuntimeError):
    pass


class BrowserPage:
    """One leased browser tab with structured action receipts.

    Public interaction primitives are writes.  A trusted read adapter may use
    clicks/fills for pagination or filters by passing ``trusted_read=True``;
    the adapter command remains the audited authorization boundary.
    """

    def __init__(self, lease: BrowserSessionLease, *, sender=None,
                 default_active: bool = False):
        from .queue import send_browser_command

        self.lease = lease
        self._send = sender or send_browser_command
        self._url = ''
        self._default_active = bool(default_active)

    @property
    def tab_id(self) -> int | None:
        return self.lease.tab_id

    @property
    def current_url(self) -> str:
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close(reason='error' if exc else 'complete')
        return False

    def _require(self, *capabilities) -> None:
        if not self.lease.active:
            raise BrowserCommandError('Browser lease has already been released')
        if self.lease.expires_at and self.lease.expires_at <= time.time():
            release_browser_lease(
                self.lease, reason='timeout', sender=self._send)
            raise BrowserCommandError('Browser lease expired')
        require_capabilities(self.lease.client_id, capabilities)

    def _authorize(self, access: str, url: str | None = None) -> None:
        target = url or self._url
        if not target:
            # An already-bound tab may not have been read yet. page_state will
            # populate it; until then only lifecycle calls are allowed.
            return
        require_access(
            self.lease.owner_user_id, target, access=access,
            client_id=self.lease.client_id, profile=self.lease.profile)

    def _state(self) -> dict:
        if self.tab_id is None:
            return {'tabId': None, 'url': self._url}
        result, error = self._send(
            'page_state', {'tabId': self.tab_id}, timeout=8,
            client_id=self.lease.client_id,
            owner_user_id=self.lease.owner_user_id)
        if error:
            return {'tabId': self.tab_id, 'url': self._url, 'stateError': str(error)}
        if isinstance(result, dict):
            self._url = str(result.get('url') or self._url)
            return result
        return {'tabId': self.tab_id, 'url': self._url}

    def _run(self, command: str, params: dict | None = None, *,
             capability: BrowserCapability | str | None = None,
             access: str | None = 'read', timeout: int = 25,
             trusted_read: bool = False, receipt: bool = True) -> dict:
        if capability:
            # BrowserPage receipts depend on protocol-v2 page_state/stable-ref
            # support. ``snapshot`` is the negotiated v2 sentinel, so a v1
            # client fails before any action is enqueued instead of performing
            # a click and only then discovering it cannot return page state.
            self._require(capability, BrowserCapability.SNAPSHOT)
        actual_access = 'read' if trusted_read else access
        if actual_access is not None:
            # Receipts describe the URL *after* the preceding action, but the
            # page can redirect on its own between actions. Refresh immediately
            # before dispatch so a redirect cannot inherit either a read allow
            # or a durable write grant from the previous origin.
            if self.tab_id is not None and command != 'navigate':
                before = self._state()
                if before.get('stateError') or not before.get('url'):
                    raise BrowserCommandError(
                        'Cannot verify current page URL before browser action')
            self._authorize(actual_access)
        payload = dict(params or {})
        if actual_access is not None and command != 'navigate' and self._url:
            expected_domain = normalize_domain(self._url)
            if expected_domain:
                payload['expectedDomain'] = expected_domain
        if self.tab_id is not None and 'tabId' not in payload:
            payload['tabId'] = self.tab_id
        result, error = self._send(
            command, payload, timeout=timeout, client_id=self.lease.client_id,
            owner_user_id=self.lease.owner_user_id)
        if error:
            raise BrowserCommandError(str(error))
        state = None
        if isinstance(result, dict) and isinstance(result.get('page'), dict):
            state = result.get('page')
        elif receipt:
            state = self._state()
        if isinstance(state, dict):
            self._url = str(state.get('url') or self._url)
            if actual_access is not None:
                # A successful action may navigate or redirect. The action's
                # write authority belongs to the origin checked above; the
                # destination receives read/navigation access only and must
                # pass its own policy before the receipt is returned.
                self._authorize('read')
        return {
            'ok': True, 'result': result, 'page': state,
            'lease': self.lease.public_dict(),
        }

    def new_tab(self, url: str = 'about:blank', *, active: bool | None = None) -> dict:
        self._require(BrowserCapability.TABS, BrowserCapability.SNAPSHOT)
        if url != 'about:blank':
            self._authorize('read', url=url)
        result, error = self._send(
            'create_tab', {'url': url, 'active': self._default_active
                           if active is None else bool(active),
                           'waitForLoad': url != 'about:blank',
                           'timeoutMs': 15_000}, timeout=20,
            client_id=self.lease.client_id,
            owner_user_id=self.lease.owner_user_id)
        if error:
            raise BrowserCommandError(str(error))
        if not isinstance(result, dict) or result.get('id') is None:
            raise BrowserCommandError('Browser did not return the created tab id')
        bind_lease_tab(self.lease, int(result['id']))
        self._url = str(result.get('url') or url)
        state = self._state()
        if url != 'about:blank':
            self._authorize('read')
        return {'ok': True, 'result': result, 'page': state,
                'lease': self.lease.public_dict()}

    def bind(self, tab_id: int) -> dict:
        self._require(BrowserCapability.TABS, BrowserCapability.SNAPSHOT)
        bind_lease_tab(self.lease, int(tab_id))
        state = self._state()
        self._authorize('read')
        return {'ok': True, 'result': state, 'page': state,
                'lease': self.lease.public_dict()}

    def bind_active(self) -> dict:
        """Bind the lease to this browser's active ordinary HTTP(S) tab."""
        self._require(BrowserCapability.TABS, BrowserCapability.SNAPSHOT)
        result, error = self._send(
            'list_tabs', {}, timeout=8, client_id=self.lease.client_id,
            owner_user_id=self.lease.owner_user_id)
        if error:
            raise BrowserCommandError(str(error))
        tabs = result if isinstance(result, list) else []
        tab = next((row for row in tabs if row.get('active')), None)
        if tab is None or tab.get('id') is None:
            raise BrowserCommandError('Browser has no active page tab')
        self._url = str(tab.get('url') or '')
        return self.bind(int(tab['id']))

    def switch(self, tab_id: int | None = None) -> dict:
        if tab_id is not None:
            self.bind(tab_id)
        return self._run('update_tab', {'active': True},
                         capability=BrowserCapability.TABS)

    def close_tab(self) -> dict:
        if self.tab_id is None:
            return {'ok': True, 'result': {'closed': []}, 'page': None,
                    'lease': self.lease.public_dict()}
        tab_id = self.tab_id
        out = self._run('close_tab', {'tabId': tab_id},
                        capability=BrowserCapability.TABS, access=None,
                        receipt=False)
        bind_lease_tab(self.lease, None)
        return out

    def navigate(self, url: str, *, wait: bool = True) -> dict:
        self._require(BrowserCapability.NAVIGATE)
        self._authorize('read', url=url)
        self._url = url
        return self._run('navigate', {'url': url, 'waitForLoad': bool(wait)},
                         capability=BrowserCapability.NAVIGATE, timeout=35)

    def snapshot(self, *, max_elements: int = 250, frame_id: int | None = None) -> dict:
        params = {'maxElements': max(1, min(1000, int(max_elements)))}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('page_snapshot', params,
                         capability=BrowserCapability.SNAPSHOT)

    def read(self, *, selector: str = '', max_chars: int = 60_000,
             frame_id: int | None = None) -> dict:
        params = {'selector': selector or None, 'maxChars': int(max_chars)}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('read_tab', params, capability=BrowserCapability.READ,
                         timeout=30)

    @staticmethod
    def _target(selector: str = '', ref: str = '') -> dict:
        if ref:
            return {'ref': str(ref)}
        if selector:
            return {'selector': str(selector)}
        raise ValueError('selector or ref is required')

    def click(self, *, selector: str = '', ref: str = '', trusted_read=False,
              frame_id: int | None = None) -> dict:
        params = self._target(selector, ref)
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('page_click', params, capability=BrowserCapability.CLICK,
                         access='write', trusted_read=trusted_read)

    def fill(self, value: str, *, selector: str = '', ref: str = '',
             trusted_read=False, frame_id: int | None = None) -> dict:
        params = {**self._target(selector, ref), 'value': str(value)}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('page_fill', params, capability=BrowserCapability.FILL,
                         access='write', trusted_read=trusted_read)

    def press(self, keys: str, *, selector: str = '', ref: str = '',
              trusted_read=False, frame_id: int | None = None) -> dict:
        params = {'keys': str(keys)}
        if selector or ref:
            params.update(self._target(selector, ref))
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('page_press', params, capability=BrowserCapability.PRESS,
                         access='write', trusted_read=trusted_read)

    def select(self, value: str | list[str], *, selector: str = '', ref: str = '',
               trusted_read=False, frame_id: int | None = None) -> dict:
        params = {**self._target(selector, ref), 'value': value}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('page_select', params,
                         capability=BrowserCapability.SELECT, access='write',
                         trusted_read=trusted_read)

    def scroll(self, *, direction='down', amount=700, selector: str = '',
               trusted_read=False, frame_id: int | None = None) -> dict:
        params = {'direction': direction, 'amount': int(amount),
                  'selector': selector or None}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run(
            'scroll_page', params,
            capability=BrowserCapability.SCROLL, access='write',
            trusted_read=trusted_read)

    def wait(self, *, selector: str = '', condition='visible', timeout=10,
             frame_id: int | None = None) -> dict:
        params = {'selector': selector, 'condition': condition,
                  'timeout': int(float(timeout) * 1000)}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run(
            'wait_for_element', params,
            capability=BrowserCapability.WAIT,
            timeout=max(5, min(65, int(timeout) + 3)))

    def execute(self, expression: str, *, args: dict | list | None = None,
                frame_id: int | None = None, trusted_read=False) -> dict:
        params: dict[str, Any] = {'expression': str(expression), 'args': args or {}}
        if frame_id is not None:
            self._require(BrowserCapability.IFRAMES)
            params['frameId'] = int(frame_id)
        return self._run('page_execute', params,
                         capability=BrowserCapability.EXECUTE, access='write',
                         trusted_read=trusted_read)

    def start_network_capture(self, *, url_patterns=None,
                              capture_bodies: bool = True) -> dict:
        if capture_bodies:
            self._require(BrowserCapability.NETWORK_BODY)
        out = self._run(
            'network_capture_start', {
                'urlPatterns': list(url_patterns or []),
                'captureBodies': bool(capture_bodies),
            },
            capability=BrowserCapability.NETWORK_CAPTURE)
        result = out.get('result') or {}
        capture_id = result.get('captureId') if isinstance(result, dict) else None
        if capture_id:
            self.lease.network_captures.add(str(capture_id))
        return out

    def stop_network_capture(self, capture_id: str) -> dict:
        out = self._run(
            'network_capture_stop', {'captureId': str(capture_id)},
            capability=BrowserCapability.NETWORK_CAPTURE, access=None)
        self.lease.network_captures.discard(str(capture_id))
        return out

    def research(self, url: str, *, max_chars=60_000, max_scrolls=4,
                 max_pages=3, pagination='auto') -> dict:
        """Deep-read one URL in an extension-owned temporary background tab."""
        self._require(
            BrowserCapability.DEEP_COLLECT, BrowserCapability.NETWORK_BODY)
        self._authorize('read', url=url)
        result, error = self._send(
            'research_url', {
                'url': str(url),
                'maxChars': max(1_000, min(80_000, int(max_chars))),
                'maxScrolls': max(0, min(8, int(max_scrolls))),
                'maxPages': max(1, min(5, int(max_pages))),
                'pagination': str(pagination),
                'timeoutMs': 65_000,
            }, timeout=80, client_id=self.lease.client_id,
            owner_user_id=self.lease.owner_user_id)
        if error:
            raise BrowserCommandError(str(error))
        if not isinstance(result, dict) or not result.get('url'):
            raise BrowserCommandError(
                'Browser did not return a valid deep-research result')
        self._authorize('read', url=str(result['url']))
        return {'ok': True, 'result': result, 'page': {
            'url': str(result.get('url') or ''),
            'title': str(result.get('title') or ''),
        }, 'lease': self.lease.public_dict()}

    def upload(self, data_base64: str, *, filename: str,
               mime_type='application/octet-stream', selector: str = '',
               ref: str = '') -> dict:
        return self._run(
            'page_upload', {**self._target(selector, ref), 'data': data_base64,
                            'filename': filename, 'mimeType': mime_type},
            capability=BrowserCapability.UPLOAD, access='write', timeout=40)

    def wait_download(self, download_id: int, *, timeout=60) -> dict:
        return self._run(
            'wait_download', {'downloadId': int(download_id),
                              'timeoutMs': int(timeout * 1000)},
            capability=BrowserCapability.DOWNLOADS, access=None,
            timeout=max(5, int(timeout) + 3), receipt=False)

    def screenshot(self, *, full_page=True, format='png', quality=90) -> dict:
        return self._run(
            'screenshot_tab', {'fullPage': bool(full_page), 'format': format,
                               'quality': int(quality)},
            capability=BrowserCapability.SCREENSHOT, timeout=60,
            receipt=False)

    def close(self, *, reason='complete') -> None:
        release_browser_lease(self.lease, reason=reason, sender=self._send)


__all__ = ['BrowserPage', 'BrowserCommandError']
