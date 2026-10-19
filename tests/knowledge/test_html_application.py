"""Application-local parser admission and joined, interruption-safe teardown."""
import asyncio
import threading

import pytest

from lib.app.app_factory import create_base_app
from lib.app.app_lifecycle import add_shutdown_handler, add_startup_handler
from lib.knowledge.html_application import (
    ApplicationHtmlProjector, application_html_projector, register_html_projection,
)
from lib.knowledge.native_html_service import HtmlProjectionClosed

pytestmark = pytest.mark.unit


class Binding:
    profile = {'fixture': True}

    def __init__(self):
        self.calls = []
        self.closes = 0

    def project(self, value):
        assert not self.closes
        self.calls.append(value)
        return value

    def close(self):
        self.closes += 1


def test_application_registration_is_lazy_isolated_and_fixed_before_startup():
    bindings = []

    def factory():
        value = Binding()
        bindings.append(value)
        return value

    first = create_base_app('first')
    second = create_base_app('second')
    assert application_html_projector(first) is None
    owner = register_html_projection(first, factory=factory)
    other = register_html_projection(second, factory=factory)
    assert owner is register_html_projection(first, factory=factory)
    assert owner is not other and not bindings
    with pytest.raises(ValueError, match='differs'):
        register_html_projection(first, factory=lambda: Binding())

    async def exercise():
        await first.startup()
        assert await asyncio.to_thread(application_html_projector(first), 'one') == 'one'
        assert owner.profile == Binding.profile and other.profile is None
        await first.shutdown()
        await second.startup()
        await second.shutdown()

    asyncio.run(exercise())
    assert len(bindings) == 1 and bindings[0].closes == 1
    with pytest.raises(HtmlProjectionClosed):
        owner.project('after shutdown')
    with pytest.raises(HtmlProjectionClosed):
        other.project('unused closed application')


@pytest.mark.parametrize('invalid', [False, True, 0, 'native', object()])
def test_invalid_factory_is_rejected_without_registering_an_owner(invalid):
    app = create_base_app('invalid')
    with pytest.raises(TypeError, match='callable'):
        register_html_projection(app, factory=invalid)
    assert application_html_projector(app) is None


def test_late_registration_and_shutdown_name_collision_refuse():
    app = create_base_app('late')

    async def exercise():
        await app.startup()
        with pytest.raises(ValueError, match='before startup'):
            register_html_projection(app, factory=Binding)
        await app.shutdown()

    asyncio.run(exercise())
    other = create_base_app('collision')
    add_shutdown_handler(other, lambda: None, name='tofu.html-projection')
    with pytest.raises(ValueError, match='already registered'):
        register_html_projection(other, factory=Binding)
    assert application_html_projector(other) is None


@pytest.mark.parametrize('stage', ['initialization', 'projection'])
def test_shutdown_joins_accepted_work_despite_repeated_cancellation(stage):
    entered, release = threading.Event(), threading.Event()
    binding = Binding()

    def wait_for_release():
        entered.set()
        if not release.wait(5):
            raise TimeoutError('test did not release accepted work')

    def factory():
        if stage == 'initialization':
            wait_for_release()
        return binding

    def project(value):
        if stage == 'projection':
            wait_for_release()
        return Binding.project(binding, value)

    binding.project = project
    owner = ApplicationHtmlProjector(factory)

    async def exercise():
        work = asyncio.create_task(asyncio.to_thread(owner.project, 'accepted'))
        first = second = None
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            first = asyncio.create_task(owner.close())
            await asyncio.sleep(0)
            with pytest.raises(HtmlProjectionClosed):
                owner.project('late')
            second = asyncio.create_task(owner.close())
            for _ in range(2):
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done() and not binding.closes
        finally:
            release.set()
        assert await work == 'accepted'
        with pytest.raises(asyncio.CancelledError):
            await first
        await second
        await owner.close()

    asyncio.run(exercise())
    assert binding.calls == ['accepted'] and binding.closes == 1


def test_failed_close_is_joined_once_and_preserves_cancellation():
    entered, release = threading.Event(), threading.Event()
    failure = OSError('unload refused')
    binding = Binding()

    def close():
        binding.closes += 1
        entered.set()
        if not release.wait(5):
            raise TimeoutError('test did not release cleanup')
        raise failure

    binding.close = close
    owner = ApplicationHtmlProjector(lambda: binding)
    assert owner.project('before') == 'before'

    async def exercise():
        closing = asyncio.create_task(owner.close())
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        finally:
            release.set()
        with pytest.raises(BaseExceptionGroup) as caught:
            await closing
        assert isinstance(caught.value.exceptions[0], asyncio.CancelledError)
        assert caught.value.exceptions[1] is failure
        with pytest.raises(OSError) as repeated:
            await owner.close()
        assert repeated.value is failure

    asyncio.run(exercise())
    assert binding.closes == 1
    with pytest.raises(HtmlProjectionClosed):
        owner.project('late')


def test_projection_cannot_schedule_its_own_close_on_another_thread():
    binding = Binding()
    owner = ApplicationHtmlProjector(lambda: binding)
    binding.project = lambda _: asyncio.run(owner.close())
    with pytest.raises(HtmlProjectionClosed, match='own service'):
        owner.project('reentry')
    assert not binding.closes
    asyncio.run(owner.close())
    assert binding.closes == 1


def test_startup_rollback_closes_unused_owner_without_loading():
    app = create_base_app('rollback')

    def unexpected():
        raise AssertionError('unused native binding loaded')

    owner = register_html_projection(app, factory=unexpected)
    primary = RuntimeError('startup failed')

    def fail():
        raise primary

    add_startup_handler(app, fail, name='fixture-failure')

    async def exercise():
        with pytest.raises(RuntimeError) as caught:
            await app.startup()
        assert caught.value is primary
        assert owner.profile is None
        with pytest.raises(HtmlProjectionClosed):
            owner.project('after rollback')

    asyncio.run(exercise())


@pytest.mark.parametrize('failure', [KeyboardInterrupt('unload interrupted'), SystemExit('unload exited')])
def test_control_failure_reaches_lifespan_after_other_owners_close(failure):
    app = create_base_app('control-failure')
    events = []
    add_shutdown_handler(app, lambda: events.append('dependency-closed'), name='dependency')
    binding = Binding()

    def close():
        binding.closes += 1
        raise failure

    binding.close = close
    owner = register_html_projection(app, factory=lambda: binding)
    assert owner.project('owned') == 'owned'

    async def exercise():
        await app.startup()
        with pytest.raises(type(failure)) as caught:
            await app.shutdown()
        assert caught.value is failure
        assert events == ['dependency-closed'] and binding.closes == 1

    asyncio.run(exercise())


def test_failed_cleanup_task_scheduling_keeps_owner_fenced_and_retryable(monkeypatch):
    binding = Binding()
    owner = ApplicationHtmlProjector(lambda: binding)
    assert owner.project('owned') == 'owned'
    failure = RuntimeError('task scheduling refused')
    pending = []

    def refuse(coroutine):
        pending.append(coroutine)
        raise failure

    async def exercise():
        with monkeypatch.context() as scoped:
            scoped.setattr(asyncio, 'create_task', refuse)
            with pytest.raises(RuntimeError) as caught:
                await owner.close()
            assert caught.value is failure
        assert pending[0].cr_frame is None and not binding.closes
        with pytest.raises(HtmlProjectionClosed):
            owner.project('late')
        await owner.close()

    asyncio.run(exercise())
    assert binding.closes == 1
