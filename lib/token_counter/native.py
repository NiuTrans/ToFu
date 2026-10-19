"""Application-scoped pure native text adapter; no global backend selection.

The existing token façade retains model resolution, cache, request structure,
confidence and fallback behavior. NativeHost owns lazy library/encoder lifetime;
this module borrows it through the current explicit application context.
"""

import re
from contextlib import contextmanager
from contextvars import ContextVar

_NATIVE_HOST = ContextVar('tofu_native_token_host', default=None)


@contextmanager
def native_token_scope(host):
    if host is not None:
        from lib.tasks_pkg.native_host import NativeHost
        if not isinstance(host, NativeHost):
            raise TypeError('native token scope requires application host')
    token = _NATIVE_HOST.set(host)
    try:
        yield
    finally:
        _NATIVE_HOST.reset(token)


_SURROGATES = re.compile(r'[\ud800-\udfff]')


class NativeTextEncoder:
    def __init__(self, host, encoding):
        self.host, self.encoding = host, encoding

    @staticmethod
    def _compatible_text(text):
        # Match tiktoken's UnicodeEncodeError repair exactly, including paired
        # surrogates. Ordinary UTF-8 text keeps its original object identity.
        if _SURROGATES.search(text):
            return text.encode('utf-16', 'surrogatepass').decode('utf-16', 'replace')
        return str(text) if type(text) is not str else text

    def count_batch(self, texts):
        prepared = [self._compatible_text(text) for text in texts]
        return self.host.token_counter().count_batch(prepared, encoding=self.encoding)

    def count_text(self, text):
        return self.count_batch([text])[0]


def scoped_native_encoder(encoding):
    host = _NATIVE_HOST.get()
    if host is not None:
        return NativeTextEncoder(host, encoding) if host.token_count_enabled else None
    # Import remains inert: no archive reads, code loading or vocabulary setup.
    from quart import current_app, has_app_context
    if not has_app_context():
        return None
    host = current_app.extensions.get('tofu_native_host')
    if host is None or not host.token_count_enabled:
        return None
    return NativeTextEncoder(host, encoding)
