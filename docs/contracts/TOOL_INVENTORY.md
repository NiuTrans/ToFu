# Tool inventory (GENERATED — do not edit)

Generate: `python3 scripts/gen_tool_inventory.py`; guard: `tests/tools/test_tool_inventory_generated.py --check`.

Rows derive from the live registry and per-facet tables; host-specific third-party diagnostics are available with `--json`.
Registered built-in tools: **146**; compatibility-only: **12**. Current means a supported declaration, not that every request enables it.
Exposure is the declared default: eager stays direct, deferred is found through Tool Search, pinned stays direct whenever assembled, toggle is deferred until its composer toggle pins it, direct is a protocol-required direct call, and gateway is injected by the discovery backend.

## Gaps

| gap | count | meaning |
|---|---|---|
| write tool with no approval preview | 0 | specialized or generic argument disclosure must be available |
| no UI label | 112 | the raw tool name is shown in the activity line |
| no reachable handler | 0 | schema advertised to the model but nothing executes it |
| description cannot disambiguate | 6 | the model cannot tell this tool apart from its neighbours and picks the wrong one |
| confusable tool pairs | 3 | two same-category tools open with near-identical sentences, so the model picks the wrong one |

Tools whose description cannot disambiguate:

- `get_conversation` — first sentence near-duplicates a same-category sibling
- `list_conversations` — first sentence near-duplicates a same-category sibling
- `desktop_read_file` — first sentence near-duplicates a same-category sibling
- `desktop_write_file` — first sentence near-duplicates a same-category sibling
- `read_artifact` — first sentence near-duplicates a same-category sibling
- `store_artifact` — first sentence near-duplicates a same-category sibling

Confusable same-category tool pairs (first-sentence overlap >= 0.5):

- [swarm] `read_artifact` vs `store_artifact` — overlap 0.83, shared: artifact, data, read, shared, store
- [desktop] `desktop_read_file` vs `desktop_write_file` — overlap 0.57, shared: computer, file, local, user
- [conversation] `get_conversation` vs `list_conversations` — overlap 0.5, shared: conversation, explicitly, information, past, user

## Built-in tools

| tool | category | spec | state | exposure | dispatch | write | idempotent | label | approval_enricher | approval_preview | serial | read_gate | fresh_gate | streamable | arg_repair | describes_ok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| publish_response_asset | artifacts | rich_response | current | deferred | EXACT | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| publish_response_file | artifacts | rich_response | current | deferred | EXACT | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| publish_rich_response | artifacts | rich_response | current | deferred | EXACT | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| read_tool_artifact | artifacts | tool_result_artifacts | current | eager | SET |  | ✓ |  |  |  |  |  |  |  | ✓ | ✓ |
| search_tool_artifact | artifacts | tool_result_artifacts | current | eager | SET |  | ✓ |  |  |  |  |  |  |  | ✓ | ✓ |
| kill | background | background | current | pinned | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| poll | background | background | current | pinned | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| browser_check | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_click | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_close_tab | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_devtools | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_download_url_to_server | browser | browser_download | current | deferred | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| browser_execute_js | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_fill_form | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| browser_get_cookies | browser | browser | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_get_history | browser | browser | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_list_tabs | browser | browser | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_menu_click | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| browser_navigate | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_press_key | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_preview_page | browser | page_preview | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| browser_read_page | browser | browser | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_research_page | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_screenshot | browser | browser | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_scroll | browser | browser | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_select_option | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_site | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_type | browser | browser | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| channel_configure_thread | channels | channels | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| channel_outbox | channels | channels | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| channel_outbox_approve | channels | channels | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| channel_read | channels | channels | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| channel_send | channels | channels | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| channel_subscribe | channels | channels | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| channel_threads | channels | channels | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| channel_unsubscribe | channels | channels | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| consider_project_message | conversation | project_integration | current | deferred | SET | ✓ |  |  |  | generic |  |  |  |  |  | ✓ |
| get_conversation | conversation | conv_ref | current | eager | SET |  |  |  |  |  |  |  |  |  |  |  |
| get_project_messages | conversation | project_integration | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| integration_checkpoint | conversation | project_integration | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| integration_submit | conversation | project_integration | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| list_conversations | conversation | conv_ref | current | eager | SET |  |  |  |  |  |  |  |  |  |  |  |
| send_project_message | conversation | project_integration | current | deferred | SET | ✓ |  |  |  | generic |  |  |  |  |  | ✓ |
| await_project_workers | coordination | coordinator | current | eager | SET |  |  | ✓ |  |  |  |  |  |  |  | ? |
| cancel_project_work | coordination | coordinator | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| control_conversation | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| create_conversation | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| delegate_project_work | coordination | coordinator | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| dispatch_project_assignment | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| finish_project_request | coordination | coordinator | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| get_conversation_status | coordination | coordinator | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| get_project_assignment | coordination | coordinator | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| get_project_workers | coordination | coordinator | current | eager | SET |  |  | ✓ |  |  |  |  |  |  |  | ? |
| get_router_projects | coordination | coordinator | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| report_to_user | coordination | coordinator | current | eager | SET | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| request_user_decision | coordination | coordinator | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| review_project_work | coordination | coordinator | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| route_to_project | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| router_instruction_cancel | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| router_instruction_create | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| router_instruction_list | coordination | coordinator | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| update_task_progress | coordination | coordinator | current | eager | SET | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| watchdog_configure_review | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| watchdog_message | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| watchdog_notify_router | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| watchdog_resolve_signals | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| watchdog_schedule_recheck | coordination | coordinator | current | eager | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| watchdog_snapshot | coordination | coordinator | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ? |
| await_device | desktop | device_wait | current | direct | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| desktop_clipboard | desktop | desktop | current | toggle | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_gui_action | desktop | desktop | current | toggle | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_list_files | desktop | desktop | current | toggle | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_open_app | desktop | desktop | current | toggle | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| desktop_open_file | desktop | desktop | current | toggle | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| desktop_read_file | desktop | desktop | current | toggle | SET |  |  |  |  |  |  |  |  |  |  |  |
| desktop_run_command | desktop | desktop | current | toggle | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| desktop_screenshot | desktop | desktop | current | toggle | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_system_info | desktop | desktop | current | toggle | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_write_file | desktop | desktop | current | toggle | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  |  |
| evolution_artifact | evolution | evolution_artifact | current | direct | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_coordinate | evolution | evolution | current | direct | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_explore | evolution | evolution | current | deferred | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_material | evolution | evolution | current | deferred | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_reindex | evolution | evolution | current | deferred | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_review | evolution | evolution_review | current | direct | EXACT |  |  | ✓ |  |  |  |  |  |  |  | ? |
| evolution_source | evolution | evolution_source | current | eager | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| evolution_teach | evolution | evolution | current | deferred | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| get_exploration | evolution | chat_exploration | current | deferred | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| manage_exploration | evolution | chat_exploration | current | deferred | EXACT | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| inspect_tool_effect | general | tool_effect_inspection | current | eager | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| ask_human | human | human_guidance | current | eager | EXACT |  |  | ✓ |  |  | ✓ |  |  |  |  | ✓ |
| search_knowledge | knowledge | knowledge | current | deferred | EXACT |  | ✓ | ✓ |  |  |  |  |  |  |  | ? |
| call_mcp_read_tool | mcp | mcp | compatibility |  | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| call_mcp_write_tool | mcp | mcp | compatibility |  | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| search_mcp_tools | mcp | mcp | compatibility |  | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ? |
| create_memory | memory | memory | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ✓ |
| delete_memory | memory | memory | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| merge_memories | memory | memory | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| read_working_facts | memory | working_facts | current | eager | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| record_working_fact | memory | working_facts | current | deferred | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| search_memories | memory | memory | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| update_memory | memory | memory | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| apply_diff | project | project | compatibility |  | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| apply_diffs | project | project | compatibility |  | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| check_background_command | project | project | compatibility |  | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ? |
| edit_file | project | project | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  |  | ✓ |
| find_files | project | project | current | eager | SET |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| grep_search | project | project | compatibility |  | SET |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ? |
| insert_content | project | project | compatibility |  | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| insert_contents | project | project | compatibility |  | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| inspect_image | project | inspect_image | current | eager | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ✓ |
| inspect_video | project | inspect_video | current | eager | EXACT |  |  | ✓ |  |  |  |  |  |  |  | ✓ |
| kill_background_command | project | project | compatibility |  | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| read_files | project | read_files | current | eager | EXACT |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| repo_map | project | project | current | deferred | SET |  | ✓ |  |  |  |  |  |  |  |  | ✓ |
| run_command | project | project | current | eager | SET | ✓ |  |  | ✓ | specialized |  |  |  |  | ✓ | ✓ |
| search_files | project | project | current | eager | SET |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| write_file | project | project | current | eager | SET | ✓ |  | ✓ | ✓ | specialized |  |  | ✓ |  | ✓ | ✓ |
| await_task | scheduler | scheduler | compatibility |  | SET |  |  |  |  |  | ✓ |  |  |  |  | ? |
| schedule_create | scheduler | scheduler | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| schedule_list | scheduler | scheduler | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| schedule_manage | scheduler | scheduler | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| timer_create | scheduler | timer_watcher | current | direct | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| timer_manage | scheduler | scheduler | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| fetch_url | search | fetch | current | eager | EXACT |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| update_search_settings | search | search_settings | current | deferred | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| web_search | search | search | current | eager | EXACT |  | ✓ | ✓ |  |  |  |  |  | ✓ |  | ✓ |
| load_skill | skills | skills | current | eager | SET |  | ✓ | ✓ |  |  |  |  |  |  | ✓ | ✓ |
| read_skill_resource | skills | skills | current | eager | SET |  | ✓ | ✓ |  |  |  |  |  |  |  | ✓ |
| request_skill_install | skills | skill_install | current | deferred | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ✓ |
| search_skills | skills | skills | current | eager | SET |  | ✓ | ✓ |  |  |  |  |  |  |  | ✓ |
| await_agents | swarm | swarm | compatibility |  | SET |  |  |  |  |  |  |  |  |  |  | ? |
| get_agent_messages | swarm | swarm | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| get_agent_result | swarm | swarm | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| list_agents | swarm | retained_agents | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| list_artifacts | swarm | swarm | contextual |  | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| manage_agents | swarm | retained_agents | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| read_artifact | swarm | swarm | contextual |  | SET |  |  |  |  |  |  |  |  |  |  |  |
| send_agent_message | swarm | swarm | current | deferred | SET | ✓ |  |  |  | generic |  |  |  |  |  | ✓ |
| set_approval_delegate | swarm | swarm | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| spawn_agents | swarm | swarm | current | eager | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| store_artifact | swarm | swarm | contextual |  | SET |  |  |  |  |  |  |  |  |  |  |  |
| todo_write | task | todo | current | deferred | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| execute_program | tools | tool_gateway | current | gateway | EXACT |  |  | ✓ |  |  |  |  |  |  |  | ? |
| execute_tools | tools | tool_gateway | current | gateway | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| search_tools | tools | tool_gateway | current | gateway | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ? |

## Bundled optional plugins

| tool | category | spec | state | exposure | dispatch | write | idempotent | label | approval_enricher | approval_preview | serial | read_gate | fresh_gate | streamable | arg_repair | describes_ok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| generate_image | image | image_gen | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| local_serve_deploy | local_serve | local_serve | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| local_serve_list | local_serve | local_serve | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_prepare | local_serve | local_serve | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_recommend | local_serve | local_serve | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_remove | local_serve | local_serve | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| local_serve_status | local_serve | local_serve | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_stop | local_serve | local_serve | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| paper_build | project | paper_build | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_check | project | paper_quality | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_citation_audit | project | paper_citations | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_cite_insert | project | paper_citations | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| paper_cite_search | project | paper_citations | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_import_arxiv | project | paper_build | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| paper_layout | project | paper_build | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_propose_edits | project | paper_build | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_review | project | paper_quality | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_scaffold | project | paper_quality | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| edit_slides | video | produce_edits | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| edit_video | video | produce_edits | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| motion_video_check | video | motion_video | current | deferred | SET | ✓ |  |  |  | generic |  |  |  |  |  | ✓ |
| motion_video_concat | video | motion_video | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_env_check | video | motion_video | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| motion_video_mux | video | motion_video | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_narrate | video | motion_video | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_probe | video | motion_video | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| motion_video_render | video | motion_video | current | deferred | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_storyboard_check | video | motion_video | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| patch_slides | video | produce_edits | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_report | video | produce | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_research | video | produce | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_slides | video | produce | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_video | video | produce | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| restyle_slides | video | produce_edits | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| revert_slides | video | produce_edits | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |
| revert_video | video | produce_edits | current | deferred | SET |  |  |  |  |  |  |  |  |  |  | ? |

## Plugin tools (diagnostic — NOT pinned by --check)

Inspect host-specific third-party tool rows with `python3 scripts/gen_tool_inventory.py --json`.
