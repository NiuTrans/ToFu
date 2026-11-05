"""Borrow an issued native parent through a detached input-policy carrier.

Entry points: share_native_parent, bind_native_parent, native_parent_binding
and resolve_native_parent. Frozen assignment config/tool grants stay on the
carrier; native route/account admission receives the exact original execution
object. Nothing here executes or restores a task. Bindings are process-local,
never serialized or accepted from request config.

Native delegation authority is scoped to the issuing task's live turn: its
route, fee receiver and shared account end at its terminal stamp. Sharing binds
identity only, so projections of unstarted work keep working; resolution admits
a child solely while the issuer is pending/running and otherwise refuses with
NativeParentSettled before any carrier, account slot, route or model/tool work.
Terminal release keeps a retired port as ``None``; such a task stays a native
execution parent and never degrades into a Python one.
"""
from dataclasses import dataclass

from lib.native_binary import NativeConfigurationError

_LIVE_ISSUER_STATUSES = frozenset({'pending', 'running'})


class NativeParentSettled(NativeConfigurationError):
    """The issuing turn settled before this delegated child could start.

    A typed, user-facing refusal: nothing was allocated and no model or tool
    work ran. A live turn may delegate the same work again.
    """
    reason = 'native_parent_settled'

    def __init__(self, message=(
            'native_parent_settled: this worker did not start because its spawning '
            'turn ended first, and native execution authority ends with that turn. '
            'No model or tool work ran. Delegate it again from a live turn and '
            'wait for delegated workers before ending that turn.')):
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class NativeParentBinding:
    task: dict
    invocation: object

    def admit(self, carrier):
        """Exact principal/account/task identity, independent of issuer lifetime."""
        from lib.identity import PrincipalContext
        parent, native = self.task, self.invocation
        if (carrier.get('id') != parent.get('id')
                or carrier.get('convId') != parent.get('convId')
                or type(carrier.get('_userId')) is not int
                or carrier['_userId'] != parent.get('_userId')
                or carrier.get('_executionBudget') is not parent.get('_executionBudget')
                or str(carrier.get('_tenant_id') or '') != str(parent.get('_tenant_id') or '')
                or PrincipalContext.from_payload(carrier.get('_principalContext')) != native.principal
                or PrincipalContext.from_payload(parent.get('_principalContext')) != native.principal):
            raise NativeConfigurationError('Detached native parent binding changed')

    def resolve(self, carrier):
        self.admit(carrier)
        parent, native = self.task, self.invocation
        port = parent.get('_nativePreparedInvocation')
        if parent.get('status') not in _LIVE_ISSUER_STATUSES and port in (None, native):
            raise NativeParentSettled()
        if port is not native:
            raise NativeConfigurationError('Detached native parent binding changed')
        return native, parent


def native_parent_binding(source):
    """Return the issued parent a task or carrier carries, admitting no work.

    Python tasks return None. A retired port cannot be bound again: a settled
    native task refuses instead of silently looking like a Python parent.
    """
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    if '_nativeInvocationParent' in source:
        binding = source['_nativeInvocationParent']
        if (not isinstance(binding, NativeParentBinding)
                or not isinstance(binding.invocation, NativePreparedInvocation)
                or '_nativePreparedInvocation' in source):
            raise NativeConfigurationError('Detached native parent requires one issued binding')
        binding.admit(source)
        return binding
    if '_nativePreparedInvocation' not in source:
        return None
    native = source['_nativePreparedInvocation']
    if native is None and source.get('status') not in _LIVE_ISSUER_STATUSES:
        raise NativeParentSettled()
    if not isinstance(native, NativePreparedInvocation):
        raise NativeConfigurationError('Child native parent requires an issued invocation port')
    return NativeParentBinding(source, native)


def resolve_native_parent(carrier):
    """Admit the exact live issuer for one child, or refuse before allocation."""
    binding = native_parent_binding(carrier)
    if binding is None:
        return None, carrier
    if binding.task is carrier:
        if carrier.get('status') not in _LIVE_ISSUER_STATUSES:
            raise NativeParentSettled()
        return binding.invocation, carrier
    return binding.resolve(carrier)


def bind_native_parent(binding, carrier):
    """Attach a binding captured while its issuer was live; use admits liveness."""
    if binding is None:
        return
    if carrier is binding.task or carrier.get('_nativePreparedInvocation') not in (None, binding.invocation):
        raise ValueError('Native parent sharing requires a distinct compatible carrier')
    binding.admit(carrier)
    carrier.pop('_nativePreparedInvocation', None)
    carrier['_nativeInvocationParent'] = binding


def share_native_parent(parent, carrier):
    """Keep a live execution reference separate from copied assignment policy."""
    bind_native_parent(native_parent_binding(parent), carrier)
