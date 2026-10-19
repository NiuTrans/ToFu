"""Borrow an issued native parent through a detached input-policy carrier.

Entry points: share_native_parent and resolve_native_parent. Frozen assignment
config/tool grants stay on the carrier; native route/account admission receives
the exact original execution object. Nothing here executes or restores a task.
Bindings are process-local, never serialized or accepted from request config.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NativeParentBinding:
    task: dict
    invocation: object

    def resolve(self, carrier):
        from lib.agent_core.native_process import NativeConfigurationError
        from lib.identity import PrincipalContext
        parent, native = self.task, self.invocation
        if (parent.get('_nativePreparedInvocation') is not native
                or carrier.get('id') != parent.get('id')
                or carrier.get('convId') != parent.get('convId')
                or type(carrier.get('_userId')) is not int
                or carrier['_userId'] != parent.get('_userId')
                or carrier.get('_executionBudget') is not parent.get('_executionBudget')
                or str(carrier.get('_tenant_id') or '') != str(parent.get('_tenant_id') or '')
                or PrincipalContext.from_payload(carrier.get('_principalContext')) != native.principal
                or PrincipalContext.from_payload(parent.get('_principalContext')) != native.principal):
            raise NativeConfigurationError('Detached native parent binding changed')
        return native, parent


def resolve_native_parent(carrier):
    if '_nativeInvocationParent' not in carrier and carrier.get('_nativePreparedInvocation') is None:
        return None, carrier
    from lib.agent_core.native_process import NativeConfigurationError
    from lib.tasks_pkg.native_invocation import NativePreparedInvocation
    if '_nativeInvocationParent' in carrier:
        binding = carrier['_nativeInvocationParent']
        if (not isinstance(binding, NativeParentBinding)
                or not isinstance(binding.invocation, NativePreparedInvocation)
                or '_nativePreparedInvocation' in carrier):
            raise NativeConfigurationError('Detached native parent requires one issued binding')
        return binding.resolve(carrier)
    native = carrier.get('_nativePreparedInvocation')
    if native is not None and not isinstance(native, NativePreparedInvocation):
        raise NativeConfigurationError('Child native parent requires an issued invocation port')
    return native, carrier


def share_native_parent(parent, carrier):
    """Keep a live execution reference separate from copied assignment policy."""
    native, execution_parent = resolve_native_parent(parent)
    if native is None:
        return
    if carrier is execution_parent or carrier.get('_nativePreparedInvocation') not in (None, native):
        raise ValueError('Native parent sharing requires a distinct compatible carrier')
    binding = NativeParentBinding(execution_parent, native)
    binding.resolve(carrier)
    carrier.pop('_nativePreparedInvocation', None)
    carrier['_nativeInvocationParent'] = binding
