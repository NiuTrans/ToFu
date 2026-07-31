"""routes/api_v1/ — Tofu native v1 API surface.

Mounted at ``/api/v1/*``. All routes use:
  - Bearer-token auth (or ``TUNNEL_TOKEN`` cookie/header for the UI)
  - Per-key rate limits + idempotency (where it makes sense)
  - ``@api_meta(...)`` for OpenAPI 3.1 self-description
  - ``@require_scope('...')`` for scope-based access control

Sub-blueprints:
  * ``chat``           — POST /chat/completions (sync/SSE/WS)
  * ``tasks``          — generic task lifecycle for any kind
  * ``conversations``  — CRUD + branches + search (delegates to legacy)
  * ``capabilities``   — self-describing surface (models, tools, presets)
  * ``keys``           — admin-scoped API key management
  * ``agents``         — paper/translate/swarm/scheduler/memory/etc.
  * ``webhooks``       — outbound event delivery
"""

from .auth import (  # noqa: F401  — re-exports for tests/clients
    bearer_auth_before_request,
    require_scope,
    require_auth,
    current_auth,
)

# Import sub-blueprints. Each sub-module registers routes on its own
# Blueprint and exposes it as ``<name>_bp``.
from .chat import api_v1_chat_bp
from .chat_direct import api_v1_chat_direct_bp
from .tasks import api_v1_tasks_bp
from .capabilities import api_v1_capabilities_bp
from .keys import api_v1_keys_bp
from .auth_mode import api_v1_auth_mode_bp
from .billing import api_v1_billing_bp
from .users import api_v1_users_bp
from .conversations import api_v1_conversations_bp
from .agents import api_v1_agents_bp
from .agent_run import api_v1_agent_run_bp
from .folders import api_v1_folders_bp
from .orchestrations import api_v1_orchestrations_bp
from .optimizer import api_v1_optimizer_bp
from .scheduler import api_v1_scheduler_bp
from .endpoint import api_v1_endpoint_bp
from .swarm import api_v1_swarm_bp
from .desktop import api_v1_desktop_bp
from .browser import api_v1_browser_bp
from .auth_sources import api_v1_auth_sources_bp
from .private_hosts import api_v1_private_hosts_bp
from .memory import api_v1_memory_bp
from .skills import api_v1_skills_bp
from .mcp import api_v1_mcp_bp
from .daily_report import api_v1_daily_report_bp
from .oauth import api_v1_oauth_bp
from .project import api_v1_project_bp
from .translate import api_v1_translate_bp
from .artifacts import api_v1_artifacts_bp
from .paper import api_v1_paper_bp
from .research import api_v1_research_bp
from .paper_folders import api_v1_paper_folders_bp
from .motion import api_v1_motion_bp
from .uploads import api_v1_uploads_bp
from .audio import api_v1_audio_bp
from .common import api_v1_common_bp
from .config import api_v1_config_bp
from .providers import api_v1_providers_bp
from .webhooks import api_v1_webhooks_bp
from .usage import api_v1_usage_bp
from .logs import api_v1_logs_bp
from .update import api_v1_update_bp


ALL_V1_BLUEPRINTS = [
    api_v1_chat_bp,
    api_v1_chat_direct_bp,
    api_v1_tasks_bp,
    api_v1_research_bp,
    api_v1_capabilities_bp,
    api_v1_keys_bp,
    api_v1_auth_mode_bp,
    api_v1_billing_bp,
    api_v1_users_bp,
    api_v1_conversations_bp,
    api_v1_agents_bp,
    api_v1_agent_run_bp,
    api_v1_folders_bp,
    api_v1_orchestrations_bp,
    api_v1_optimizer_bp,
    api_v1_scheduler_bp,
    api_v1_endpoint_bp,
    api_v1_swarm_bp,
    api_v1_desktop_bp,
    api_v1_browser_bp,
    api_v1_auth_sources_bp,
    api_v1_private_hosts_bp,
    api_v1_memory_bp,
    api_v1_skills_bp,
    api_v1_mcp_bp,
    api_v1_daily_report_bp,
    api_v1_oauth_bp,
    api_v1_project_bp,
    api_v1_translate_bp,
    api_v1_artifacts_bp,
    api_v1_paper_bp,
    api_v1_paper_folders_bp,
    api_v1_motion_bp,
    api_v1_uploads_bp,
    api_v1_audio_bp,
    api_v1_common_bp,
    api_v1_config_bp,
    api_v1_providers_bp,
    api_v1_webhooks_bp,
    api_v1_usage_bp,
    api_v1_logs_bp,
    api_v1_update_bp,
]
