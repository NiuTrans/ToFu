"""Developer-facing Tofu agent runtime.

The public package intentionally contains no database or full Tofu application
lifecycle. It composes the same orchestrator and model-routing v2 contract used
by the full application.
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
    ModelRoutingConfig,
)
from tofu_agent.runtime import AgentExecution, AgentRuntime
from tofu_agent.provider_store import ModelRoutingSettingsStore

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
    'ModelRoutingConfig',
    'ModelRoutingSettingsStore',
    '__api_version__',
    '__version__',
]
