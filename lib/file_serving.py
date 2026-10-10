"""lib/file_serving.py — conditional (Range-aware) file serving, one seam.

WHY THIS SEAM EXISTS (measured 2026-08-02, live on the desktop-installer
download route; independently verified twice)
-----------------------------------------------
With the installed quart / werkzeug 3.1.8 pair, ``send_file(...,
conditional=True)`` 500s on a SINGLE-BYTE Range (``bytes=0-0``,
``bytes=5-5`` — the classic "does this server resume?" probe every
download manager sends). Chain: quart's ``_process_range_request`` passes
``end - 1`` as the inclusive stop into ``ContentRange.set``, and
werkzeug's ``is_byte_range_valid`` rejects ``start >= stop`` outright —
so ``begin == end - 1`` raises ``AssertionError: Bad range provided``,
which escapes as an uncaught 500. Multi-byte ranges (``bytes=0-99``) and
unsatisfiable ones (416) work fine; plain GETs are unaffected.

``send_file_conditional`` is the single seam every file-serving route
calls instead of send_file directly. Behaviour:

  * identical to ``send_file(..., conditional=True)`` for every input
    except that exact AssertionError;
  * that AssertionError degrades to a plain full-body 200 (a spec-legal
    answer to any GET — a server may always ignore a Range header),
    logged with the path, never a 500.

The catch is narrowed to the werkzeug message so no OTHER assertion
silently turns into a 200.

Sync-shaped and call-time-imported ON PURPOSE: :mod:`lib.quart_sync` is the
explicit boundary that resolves Quart's async ``send_file`` from executor
threads. Quart itself remains untouched, while legacy synchronous routes still
receive a concrete Response rather than a coroutine object.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def send_file_conditional(path, **kwargs):
    """``send_file(path, conditional=True, **kwargs)`` without the 500.

    Drop-in for synchronous routes' current
    ``send_file(..., conditional=True)`` calls (see module docstring).
    """
    from lib.quart_sync import send_file as _send_file
    kwargs['conditional'] = True
    try:
        return _send_file(path, **kwargs)
    except AssertionError as e:
        if 'Bad range provided' not in str(e):
            raise
        logger.warning('[FileServing] conditional send_file failed for %s '
                       '(single-byte Range probe; see module docstring) — '
                       'falling back to full-body 200', path)
        kwargs['conditional'] = False
        return _send_file(path, **kwargs)


__all__ = ['send_file_conditional']


def send_leased_artifact(lease, *, mimetype, buffer_size=65536):
    """Serve leased seekable bytes without materializing a file or full buffer.

    Werkzeug owns conditional/Range semantics, including one-byte and suffix
    requests. Blocking reads and teardown run off the serving loop. Cancellation
    joins an outstanding read before closing its lease; an unstarted body also
    releases its lease on collection through the existing executor.
    """
    import asyncio
    import contextvars
    import weakref
    from contextlib import suppress
    from quart import current_app, request
    from quart.wrappers.response import ResponseBody
    from werkzeug.wrappers import Response as HTTPResponse

    async def io_call(function, *args):
        # A shielded executor Future also survives repeated cancellation; a
        # second cancel must not close a stream while its worker still reads.
        future = asyncio.get_running_loop().run_in_executor(
            None, contextvars.copy_context().run, function, *args)
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException as exc:
                logger.debug('leased artifact stream await failed: %s',
                             type(exc).__name__)
                break
        if cancelled:
            with suppress(BaseException):
                future.result()
            raise asyncio.CancelledError
        return future.result()

    def release_unstarted():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            lease.__exit__(None, None, None)
        else:
            loop.run_in_executor(None, lease.__exit__, None, None, None)

    class LeasedBody(ResponseBody):
        def __init__(self, stream, remaining):
            self.stream, self.remaining = stream, remaining
            self.release = weakref.finalize(self, release_unstarted)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            await io_call(self.release)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.remaining:
                raise StopAsyncIteration
            data = await io_call(self.stream.read, min(buffer_size, self.remaining))
            if not data or len(data) > self.remaining:
                raise OSError('Artifact ended before its declared byte range')
            self.remaining -= len(data)
            return data

    stream, metadata = lease.__enter__()
    try:
        size = int(metadata['size_bytes'])
        http = HTTPResponse(mimetype=mimetype)
        http.content_length = size
        http.set_etag(metadata['sha256'])
        http.cache_control.private = True
        http.cache_control.max_age = 0
        http.cache_control.must_revalidate = True
        http.headers['Accept-Ranges'] = 'bytes'
        http.headers['X-Content-Type-Options'] = 'nosniff'
        http.headers['X-Artifact-SHA256'] = metadata['sha256']
        conditional_headers = ('Range', 'If-Range', 'If-None-Match', 'If-Match',
                               'If-Modified-Since', 'If-Unmodified-Since')
        environ = {'REQUEST_METHOD': request.method, **{
            'HTTP_' + name.upper().replace('-', '_'): request.headers[name]
            for name in conditional_headers if name in request.headers}}
        http.make_conditional(environ, accept_ranges=True, complete_length=size)
        begin, end = (http.content_range.start, http.content_range.stop) if http.status_code == 206 else (0, size)
        has_body = request.method != 'HEAD' and http.status_code in (200, 206) and end > begin
        if has_body:
            stream.seek(begin)
            if not stream.read(1):
                raise OSError('Artifact ended before its declared byte range')
            stream.seek(begin)
            body = LeasedBody(stream, end - begin)
        else:
            body = b''
            lease.__exit__(None, None, None)
        response = current_app.response_class(body, status=http.status_code, headers=http.headers)
        if http.status_code == 304:
            response.headers.pop('Content-Length', None)
        else:
            response.content_length = http.content_length if request.method == 'HEAD' or has_body else 0
        return response
    except BaseException:
        lease.__exit__(None, None, None)
        raise


__all__.append('send_leased_artifact')
