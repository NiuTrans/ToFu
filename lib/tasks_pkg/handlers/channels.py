# HOT_PATH
"""Channel tool handler: adapts unified tool calls to ``lib.channels.tools``.

The domain module owns schemas (contracts/channels_v1.schema.json), the
authority boundary and execution; this adapter only settles the result.
"""

from __future__ import annotations

from lib.channels.model import TOOL_NAMES as CHANNEL_TOOL_NAMES
from lib.tasks_pkg.executor import tool_registry
from lib.tasks_pkg.handlers._adapter import simple_call


@tool_registry.tool_set(CHANNEL_TOOL_NAMES, category='channels',
                        description='External channel threads, subscriptions and outbox (大象)')
def _handle_channel_tool(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg,
                         project_path, project_enabled, all_tools=None):
    del tc, cfg, project_path, project_enabled, all_tools
    from lib.channels.tools import execute_json
    return simple_call(task, fn_name, fn_args, rn, round_entry, tc_id,
                       executor=lambda name, args: execute_json(name, args or {}, task),
                       source='Channels', module_tag='Channels')
