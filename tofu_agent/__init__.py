"""Developer-facing Tofu agent runtime.

The public package intentionally contains no database or ChatUI application
lifecycle.  It composes the same orchestrator used by the full application and
ships a small Provider setup control plane for the ``tofu-agent`` sidecar.
"""

from lib.identity import PERSONAL_USER_ID, PrincipalContext
from tofu_agent.models import (
    AgentClosedError,
    AgentConfigurationError,
    AgentOverloadedError,
    AgentRequest,
    AgentResult,
    AgentRuntimeError,
    AgentTimeoutError,
    CUSTOM_TOOLS_MODES,
    ProviderConfig,
)
from tofu_agent.runtime import AgentExecution, AgentRuntime
from tofu_agent.provider_store import ProviderSettingsStore

try:
    from lib.version import __version__
except ImportError:  # pragma: no cover - malformed source checkout
    __version__ = 'unknown'

__api_version__ = 'v1'

__all__ = [
    'AgentClosedError',
    'AgentConfigurationError',
    'AgentExecution',
    'AgentOverloadedError',
    'AgentRequest',
    'AgentResult',
    'AgentRuntime',
    'AgentRuntimeError',
    'AgentTimeoutError',
    'CUSTOM_TOOLS_MODES',
    'PERSONAL_USER_ID',
    'PrincipalContext',
    'ProviderConfig',
    'ProviderSettingsStore',
    '__api_version__',
    '__version__',
]
