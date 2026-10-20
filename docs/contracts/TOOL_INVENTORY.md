# Tool inventory (GENERATED — do not edit)

Generate: `python3 scripts/gen_tool_inventory.py`; guard: `tests/tools/test_tool_inventory_generated.py --check`.

Rows derive from the live registry and per-facet tables; host-specific third-party diagnostics are available with `--json`.
Registered built-in tools: **118**; compatibility-only: **11**. Current means a supported declaration, not that every request enables it.

## Gaps

| gap | count | meaning |
|---|---|---|
| write tool with no approval preview | 0 | specialized or generic argument disclosure must be available |
| no UI label | 88 | the raw tool name is shown in the activity line |
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

| tool | category | spec | state | dispatch | write | idempotent | label | approval_enricher | approval_preview | serial | read_gate | fresh_gate | streamable | arg_repair | describes_ok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| publish_response_asset | artifacts | rich_response | current | EXACT | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| publish_response_file | artifacts | rich_response | current | EXACT | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| publish_rich_response | artifacts | rich_response | current | EXACT | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| read_tool_artifact | artifacts | tool_result_artifacts | current | SET |  | ✓ |  |  |  |  |  |  |  | ✓ | ✓ |
| search_tool_artifact | artifacts | tool_result_artifacts | current | SET |  | ✓ |  |  |  |  |  |  |  | ✓ | ✓ |
| kill | background | background | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| poll | background | background | current | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| browser_check | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_click | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_close_tab | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_devtools | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_download_url_to_server | browser | browser_download | current | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| browser_execute_js | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_fill_form | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| browser_get_cookies | browser | browser | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_get_history | browser | browser | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_list_tabs | browser | browser | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_menu_click | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| browser_navigate | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_press_key | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_preview_page | browser | page_preview | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| browser_read_page | browser | browser | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_research_page | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_screenshot | browser | browser | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_scroll | browser | browser | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| browser_select_option | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_site | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| browser_type | browser | browser | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| consider_project_message | conversation | project_integration | current | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| get_conversation | conversation | conv_ref | current | SET |  |  |  |  |  |  |  |  |  |  |  |
| get_project_messages | conversation | project_integration | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| integration_checkpoint | conversation | project_integration | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| integration_submit | conversation | project_integration | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| list_conversations | conversation | conv_ref | current | SET |  |  |  |  |  |  |  |  |  |  |  |
| send_project_message | conversation | project_integration | current | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| await_project_workers | coordination | coordinator | current | SET |  |  | ✓ |  |  |  |  |  |  |  | ? |
| cancel_project_work | coordination | coordinator | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| create_conversation | coordination | coordinator | current | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| delegate_project_work | coordination | coordinator | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| dispatch_project_assignment | coordination | coordinator | current | SET | ✓ |  |  |  | generic |  |  |  |  |  | ? |
| finish_project_request | coordination | coordinator | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| get_project_assignment | coordination | coordinator | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| get_project_workers | coordination | coordinator | current | SET |  |  | ✓ |  |  |  |  |  |  |  | ? |
| report_to_user | coordination | coordinator | current | SET | ✓ |  | ✓ |  | generic |  |  |  |  |  | ? |
| review_project_work | coordination | coordinator | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| desktop_clipboard | desktop | desktop | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_gui_action | desktop | desktop | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_list_files | desktop | desktop | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_open_app | desktop | desktop | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| desktop_open_file | desktop | desktop | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| desktop_read_file | desktop | desktop | current | SET |  |  |  |  |  |  |  |  |  |  |  |
| desktop_run_command | desktop | desktop | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| desktop_screenshot | desktop | desktop | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_system_info | desktop | desktop | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_write_file | desktop | desktop | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  |  |
| evolution_artifact | evolution | evolution_artifact | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_coordinate | evolution | evolution | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_explore | evolution | evolution | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_material | evolution | evolution | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_reindex | evolution | evolution | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| evolution_review | evolution | evolution_review | current | EXACT |  |  | ✓ |  |  |  |  |  |  |  | ? |
| evolution_source | evolution | evolution_source | current | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| evolution_teach | evolution | evolution | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| inspect_tool_effect | general | tool_effect_inspection | current | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| ask_human | human | human_guidance | current | EXACT |  |  | ✓ |  |  | ✓ |  |  |  |  | ✓ |
| search_knowledge | knowledge | knowledge | current | EXACT |  | ✓ | ✓ |  |  |  |  |  |  |  | ? |
| call_mcp_read_tool | mcp | mcp | compatibility | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| call_mcp_write_tool | mcp | mcp | compatibility | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| search_mcp_tools | mcp | mcp | compatibility | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ? |
| create_memory | memory | memory | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ✓ |
| delete_memory | memory | memory | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| merge_memories | memory | memory | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| read_working_facts | memory | working_facts | current | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| record_working_fact | memory | working_facts | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| search_memories | memory | memory | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| update_memory | memory | memory | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| apply_diff | project | project | compatibility | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| apply_diffs | project | project | compatibility | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| check_background_command | project | project | compatibility | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ? |
| edit_file | project | project | current | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  |  | ✓ |
| find_files | project | project | current | SET |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| grep_search | project | project | current | SET |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| insert_content | project | project | compatibility | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| insert_contents | project | project | compatibility | SET | ✓ |  | ✓ | ✓ | specialized |  | ✓ | ✓ |  | ✓ | ? |
| inspect_image | project | inspect_image | current | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ✓ |
| kill_background_command | project | project | compatibility | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| read_files | project | read_files | current | EXACT |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| repo_map | project | project | current | SET |  | ✓ |  |  |  |  |  |  |  |  | ✓ |
| run_command | project | project | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  | ✓ | ✓ |
| write_file | project | project | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  | ✓ |  | ✓ | ✓ |
| await_task | scheduler | scheduler | compatibility | SET |  |  |  |  |  | ✓ |  |  |  |  | ? |
| schedule_create | scheduler | scheduler | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| schedule_list | scheduler | scheduler | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| schedule_manage | scheduler | scheduler | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| timer_create | scheduler | timer_watcher | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| timer_manage | scheduler | scheduler | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| fetch_url | search | fetch | current | EXACT |  | ✓ | ✓ |  |  |  |  |  | ✓ | ✓ | ✓ |
| update_search_settings | search | search_settings | current | EXACT | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| web_search | search | search | current | EXACT |  | ✓ | ✓ |  |  |  |  |  | ✓ |  | ✓ |
| load_skill | skills | skills | current | SET |  | ✓ | ✓ |  |  |  |  |  |  | ✓ | ✓ |
| read_skill_resource | skills | skills | current | SET |  | ✓ | ✓ |  |  |  |  |  |  |  | ✓ |
| request_skill_install | skills | skill_install | current | SET | ✓ |  | ✓ | ✓ | specialized |  |  |  |  |  | ? |
| search_skills | skills | skills | current | SET |  | ✓ | ✓ |  |  |  |  |  |  |  | ✓ |
| await_agents | swarm | swarm | compatibility | SET |  |  |  |  |  |  |  |  |  |  | ? |
| get_agent_messages | swarm | swarm | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| get_agent_result | swarm | swarm | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| list_agents | swarm | retained_agents | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| list_artifacts | swarm | swarm | contextual | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| manage_agents | swarm | retained_agents | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| read_artifact | swarm | swarm | contextual | SET |  |  |  |  |  |  |  |  |  |  |  |
| send_agent_message | swarm | swarm | current | SET | ✓ |  |  |  | generic |  |  |  |  |  | ✓ |
| set_approval_delegate | swarm | swarm | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| spawn_agents | swarm | swarm | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| store_artifact | swarm | swarm | contextual | SET |  |  |  |  |  |  |  |  |  |  |  |
| todo_write | task | todo | current | EXACT |  |  |  |  |  |  |  |  |  |  | ✓ |
| execute_program | tools | tool_gateway | current | EXACT |  |  | ✓ |  |  |  |  |  |  |  | ? |
| execute_tools | tools | tool_gateway | current | EXACT |  |  |  |  |  |  |  |  |  |  | ? |
| search_tools | tools | tool_gateway | current | EXACT |  | ✓ |  |  |  |  |  |  |  |  | ? |

## Bundled optional plugins

| tool | category | spec | state | dispatch | write | idempotent | label | approval_enricher | approval_preview | serial | read_gate | fresh_gate | streamable | arg_repair | describes_ok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| generate_image | image | image_gen | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| local_serve_deploy | local_serve | local_serve | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| local_serve_list | local_serve | local_serve | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_prepare | local_serve | local_serve | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_recommend | local_serve | local_serve | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_remove | local_serve | local_serve | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| local_serve_status | local_serve | local_serve | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| local_serve_stop | local_serve | local_serve | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| paper_build | project | paper_build | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_check | project | paper_quality | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_citation_audit | project | paper_citations | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_cite_insert | project | paper_citations | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| paper_cite_search | project | paper_citations | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_import_arxiv | project | paper_build | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ? |
| paper_layout | project | paper_build | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_propose_edits | project | paper_build | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_review | project | paper_quality | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| paper_scaffold | project | paper_quality | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| edit_slides | video | produce_edits | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| edit_video | video | produce_edits | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| motion_video_check | video | motion_video | current | SET | ✓ |  |  |  | generic |  |  |  |  |  | ✓ |
| motion_video_concat | video | motion_video | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_env_check | video | motion_video | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| motion_video_mux | video | motion_video | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_narrate | video | motion_video | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_probe | video | motion_video | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| motion_video_render | video | motion_video | current | SET | ✓ |  |  | ✓ | specialized |  |  |  |  |  | ✓ |
| motion_video_storyboard_check | video | motion_video | current | SET |  |  |  |  |  |  |  |  |  |  | ✓ |
| patch_slides | video | produce_edits | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_report | video | produce | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_research | video | produce | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_slides | video | produce | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| produce_video | video | produce | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| restyle_slides | video | produce_edits | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| revert_slides | video | produce_edits | current | SET |  |  |  |  |  |  |  |  |  |  | ? |
| revert_video | video | produce_edits | current | SET |  |  |  |  |  |  |  |  |  |  | ? |

## Plugin tools (diagnostic — NOT pinned by --check)

Inspect host-specific third-party tool rows with `python3 scripts/gen_tool_inventory.py --json`.
