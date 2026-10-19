"""Lazy native admission has one joined owner even across failure and close."""
from concurrent.futures import ThreadPoolExecutor
import threading
import pytest

from lib.knowledge.native_html_service import HtmlProjectionClosed, LazyHtmlProjector


@pytest.mark.parametrize('reentry', ['project', 'close'])
def test_initialization_reentry_refuses_and_retires(reentry):
    service = LazyHtmlProjector(lambda: getattr(service, reentry)(*(['nested'] if reentry == 'project' else [])))
    with pytest.raises(HtmlProjectionClosed):
        service.project('first')
    with pytest.raises(HtmlProjectionClosed, match='previously failed'):
        service.project('later')
    service.close()


def test_binding_cannot_close_its_own_active_service():
    class ReentrantBinding:
        profile = {'test': True}
        def project(self, value):
            service.close()
        def close(self):
            self.closed = True
    binding = ReentrantBinding()
    service = LazyHtmlProjector(lambda: binding)
    with pytest.raises(HtmlProjectionClosed, match='own service'):
        service.project('first')
    service.close()
    assert binding.closed


def test_failed_profile_closes_acquired_binding_and_preserves_control_exception():
    failure = KeyboardInterrupt('profile interrupted')
    class Binding:
        closed = 0
        @property
        def profile(self): raise failure
        def close(self): self.closed += 1
    binding = Binding()
    service = LazyHtmlProjector(lambda: binding)
    with pytest.raises(KeyboardInterrupt) as caught: service.project('private input')
    assert caught.value is failure and binding.closed == 1
    assert service.profile is None
    service.close()
    assert binding.closed == 1

pytestmark = pytest.mark.unit


class Binding:
    profile = {'version': 'fixture'}
    closed = False
    def project(self, value):
        assert not self.closed
        return value
    def close(self):
        assert not self.closed
        self.closed = True


def test_unused_service_is_lazy_and_close_does_not_load():
    def unexpected(): raise AssertionError('unused native load')
    service = LazyHtmlProjector(unexpected)
    assert service.profile is None
    service.close(); service.close()
    with pytest.raises(HtmlProjectionClosed): service.project('late')


def test_concurrent_initialization_and_close_join_every_accepted_call():
    entered, release = threading.Event(), threading.Event()
    calls = []
    binding = Binding()
    def factory():
        calls.append('load'); entered.set()
        if not release.wait(5): raise TimeoutError('factory was not released')
        return binding
    service = LazyHtmlProjector(factory)
    with ThreadPoolExecutor(max_workers=9) as executor:
        futures = [executor.submit(service.project, str(i)) for i in range(8)]
        try:
            assert entered.wait(3)
            with service._condition:
                assert service._condition.wait_for(lambda: service._active == 8, 3)
            closed = executor.submit(service.close)
            with service._condition:
                assert service._condition.wait_for(lambda: service._state == 'closing', 3)
            assert not closed.done() and not binding.closed
            with pytest.raises(HtmlProjectionClosed): service.project('late')
        finally: release.set()
        assert [future.result(5) for future in futures] == [str(i) for i in range(8)]
        closed.result(5)
    assert calls == ['load'] and binding.closed and service.profile == binding.profile


def test_initialization_failure_is_not_retried_and_no_exception_is_cached():
    calls = []
    failure = OSError('pin rejected')
    def factory(): calls.append('load'); raise failure
    service = LazyHtmlProjector(factory)
    with pytest.raises(OSError) as caught: service.project('first')
    assert caught.value is failure
    with pytest.raises(HtmlProjectionClosed, match='previously failed'): service.project('second')
    assert calls == ['load'] and service._factory is None
    assert not any(isinstance(value, BaseException) for value in vars(service).values())
    service.close()


def test_call_error_keeps_binding_usable_but_close_error_is_retained():
    class FaultyBinding(Binding):
        def project(self, value):
            if value == 'fail': raise ValueError('projection refused')
            return super().project(value)
        def close(self): raise OSError('unload failed')
    binding = FaultyBinding()
    service = LazyHtmlProjector(lambda: binding)
    with pytest.raises(ValueError): service.project('fail')
    assert service.project('valid') == 'valid'
    with pytest.raises(OSError): service.close()
    assert service._binding is binding
    with pytest.raises(HtmlProjectionClosed, match='retained'): service.close()
    with pytest.raises(HtmlProjectionClosed): service.project('late')
