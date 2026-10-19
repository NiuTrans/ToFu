"""Application ownership for an explicitly selected lazy HTML binding.

register_html_projection installs one owner in the existing lifespan registry.
Routes borrow its synchronous project method; no application context or identity
enters the parser. The trusted factory admits pinned native bytes on first use.
Close fences new calls, joins accepted calls off the serving loop, and preserves
cancellation only after cleanup settles. Imports never load a native library.
"""
import asyncio

from .native_html_service import HtmlProjectionClosed, LazyHtmlProjector


class ApplicationHtmlProjector:
    def __init__(self, factory):
        self.factory = factory
        self._service = LazyHtmlProjector(factory)
        self._accepting = True
        self._closing = None

    @property
    def profile(self):
        return self._service.profile

    def project(self, value):
        if not self._accepting:
            raise HtmlProjectionClosed('application HTML projection closed')
        return self._service.project(value)

    async def _settle(self):
        # Control exceptions in the worker must reach the lifespan handler,
        # where remaining owners still get cleanup, rather than escape from
        # a Task callback and stop the event loop before that handler resumes.
        try:
            await asyncio.to_thread(self._service.close)
        except BaseException as error:
            return error
        return None

    async def close(self):
        self._service.validate_close_caller()
        self._accepting = False
        if self._closing is None:
            cleanup = self._settle()
            try:
                self._closing = asyncio.create_task(cleanup)
            except BaseException:
                cleanup.close()
                raise
        elif self._closing.get_loop() is not asyncio.get_running_loop():
            raise HtmlProjectionClosed('application HTML close belongs to another event loop')
        interrupted = None
        while not self._closing.done():
            try:
                await asyncio.shield(self._closing)
            except asyncio.CancelledError as error:
                if interrupted is None:
                    interrupted = error
            except BaseException:
                # Inspect the one completed task below, including a concurrent
                # caller cancellation and a release failure.
                break
        try:
            failure = self._closing.result()
            if failure is not None:
                raise failure
        except BaseException as failure:
            if interrupted is not None:
                raise BaseExceptionGroup('HTML close interrupted and failed',
                                         [interrupted, failure]) from None
            raise
        if interrupted is not None:
            raise interrupted


def register_html_projection(app, *, factory):
    """Select once before startup; the caller supplies trusted binding policy."""
    from lib.app.app_lifecycle import add_shutdown_handler, register_app_lifecycle

    if not callable(factory):
        raise TypeError('application HTML binding factory must be callable')
    previous = app.extensions.get('tofu_html_projection')
    if previous is not None:
        if previous.factory is not factory:
            raise ValueError('application HTML binding factory differs')
        return previous
    register_app_lifecycle(app)
    if app.extensions['tofu_lifecycle']['status'] != 'created':
        raise ValueError('application HTML projection must register before startup')
    owner = ApplicationHtmlProjector(factory)
    if not add_shutdown_handler(app, owner.close, name='tofu.html-projection'):
        raise ValueError('application HTML shutdown owner already registered')
    app.extensions['tofu_html_projection'] = owner
    return owner


def application_html_projector(app):
    """Borrow this application's selection, or preserve the default parser."""
    owner = app.extensions.get('tofu_html_projection')
    return None if owner is None else owner.project
