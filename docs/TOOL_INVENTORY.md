# Tool inventory (GENERATED — do not edit)

Regenerate with `python3 scripts/gen_tool_inventory.py`; CI pins it
via `tests/test_tool_inventory_generated.py --check`.

One row per BUILT-IN tool. Every column is derived from the live
registry + the per-facet tables — nothing here is hand-maintained.
Third-party plugin tools vary per deployment and are listed in the
diagnostic section at the end, which `--check` ignores.

Built-in tools: **93**

## Gaps

| gap | count | meaning |
|---|---|---|
| write tool with no approval enricher | 0 | the approval dialog renders a bare tool name — the user approves blind, which the approval module itself calls "worse than not prompting at all" |
| no UI label | 77 | the raw tool name is shown in the activity line |
| no reachable handler | 0 | schema advertised to the model but nothing executes it |
| description cannot disambiguate | 4 | the model cannot tell this tool apart from its neighbours and picks the wrong one |
| confusable tool pairs | 2 | two same-category tools open with near-identical sentences, so the model picks the wrong one |

Tools whose description cannot disambiguate:

- `desktop_read_file` — first sentence near-duplicates a same-category sibling
- `desktop_write_file` — first sentence near-duplicates a same-category sibling
- `read_artifact` — first sentence near-duplicates a same-category sibling
- `store_artifact` — first sentence near-duplicates a same-category sibling

Confusable same-category tool pairs (first-sentence overlap >= 0.5):

- [swarm] `read_artifact` vs `store_artifact` — overlap 0.83, shared: artifact, data, read, shared, store
- [desktop] `desktop_read_file` vs `desktop_write_file` — overlap 0.57, shared: computer, file, local, user


## Built-in tools

| tool | category | spec | dispatch | write | idempotent | label | approval_enricher | serial | read_gate | fresh_gate | streamable | arg_repair | describes_ok |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| browser_click | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_close_tab | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_execute_js | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_fill_form | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_get_cookies | browser | browser | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| browser_get_history | browser | browser | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| browser_list_tabs | browser | browser | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| browser_menu_click | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_navigate | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_press_key | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| browser_preview_page | browser | page_preview | SET |  |  |  |  |  |  |  |  |  | ✓ |
| browser_read_page | browser | browser | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| browser_screenshot | browser | browser | SET |  |  |  |  |  |  |  |  |  | ✓ |
| browser_type | browser | browser | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| get_conversation | conversation | conv_ref | SET |  | ✓ |  |  |  |  |  |  |  | ? |
| list_conversations | conversation | conv_ref | SET |  | ✓ |  |  |  |  |  |  |  | ? |
| project_board_block | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_board_claim | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_board_complete | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_board_post | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_board_read | conversation | conv_ref | SET |  | ✓ |  |  |  |  |  |  |  | ? |
| project_charter_propose | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_charter_read | conversation | conv_ref | SET |  | ✓ |  |  |  |  |  |  |  | ? |
| project_feed_read | conversation | conv_ref | SET |  | ✓ |  |  |  |  |  |  |  | ? |
| project_intervene | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_message | conversation | conv_ref | SET |  |  |  |  |  |  |  |  |  | ? |
| project_peer_status | conversation | conv_ref | SET |  | ✓ |  |  |  |  |  |  |  | ? |
| desktop_clipboard | desktop | desktop | SET |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_gui_action | desktop | desktop | SET |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_list_files | desktop | desktop | SET |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_open_app | desktop | desktop | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| desktop_open_file | desktop | desktop | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| desktop_read_file | desktop | desktop | SET |  |  |  |  |  |  |  |  |  |  |
| desktop_run_command | desktop | desktop | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| desktop_screenshot | desktop | desktop | SET |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_system_info | desktop | desktop | SET |  |  |  |  |  |  |  |  |  | ✓ |
| desktop_write_file | desktop | desktop | SET | ✓ |  |  | ✓ |  |  |  |  |  |  |
| ask_human | human | human_guidance | EXACT |  |  | ✓ |  | ✓ |  |  |  |  | ✓ |
| generate_image | image | image_gen | SET |  |  |  |  |  |  |  |  |  | ✓ |
| search_knowledge | knowledge | knowledge | EXACT |  | ✓ | ✓ |  |  |  |  |  |  | ? |
| call_mcp_read_tool | mcp | mcp | EXACT |  |  |  |  |  |  |  |  |  | ? |
| call_mcp_write_tool | mcp | mcp | EXACT | ✓ |  |  | ✓ |  |  |  |  |  | ? |
| search_mcp_tools | mcp | mcp | EXACT |  | ✓ |  |  |  |  |  |  |  | ? |
| create_memory | memory | memory | SET | ✓ |  | ✓ | ✓ |  |  |  |  |  | ✓ |
| delete_memory | memory | memory | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| merge_memories | memory | memory | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| search_memories | memory | memory | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| update_memory | memory | memory | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| apply_diff | project | project | SET | ✓ |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ? |
| apply_diffs | project | project | SET | ✓ |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ? |
| create_project | project | project | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| edit_file | project | project | SET | ✓ |  | ✓ | ✓ |  | ✓ | ✓ |  |  | ✓ |
| find_files | project | project | SET |  | ✓ | ✓ |  |  |  |  | ✓ | ✓ | ✓ |
| grep_search | project | project | SET |  | ✓ | ✓ |  |  |  |  | ✓ | ✓ | ✓ |
| insert_content | project | project | SET | ✓ |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ? |
| insert_contents | project | project | SET | ✓ |  | ✓ | ✓ |  | ✓ | ✓ |  | ✓ | ? |
| inspect_image | project | inspect_image | EXACT |  | ✓ |  |  |  |  |  |  |  | ✓ |
| list_dir | project | project | SET |  | ✓ | ✓ |  |  |  |  | ✓ | ✓ | ✓ |
| read_files | project | read_files | EXACT |  | ✓ | ✓ |  |  |  |  | ✓ | ✓ | ✓ |
| run_command | project | project | SET | ✓ |  |  | ✓ |  |  |  |  | ✓ | ✓ |
| write_file | project | project | SET | ✓ |  | ✓ | ✓ |  |  | ✓ |  | ✓ | ✓ |
| await_task | scheduler | scheduler | SET |  |  |  |  | ✓ |  |  |  |  | ✓ |
| schedule_create | scheduler | scheduler | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| schedule_list | scheduler | scheduler | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| schedule_manage | scheduler | scheduler | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| timer_create | scheduler | scheduler | SET | ✓ |  |  | ✓ | ✓ |  |  |  |  | ✓ |
| timer_manage | scheduler | scheduler | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| fetch_url | search | fetch | EXACT |  | ✓ | ✓ |  |  |  |  | ✓ | ✓ | ✓ |
| update_search_settings | search | search_settings | EXACT | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| web_search | search | search | EXACT |  | ✓ | ✓ |  |  |  |  | ✓ |  | ✓ |
| load_skill | skills | skills | SET |  | ✓ | ✓ |  |  |  |  |  |  | ? |
| await_agents | swarm | swarm | SET |  |  |  |  |  |  |  |  |  | ✓ |
| get_agent_result | swarm | swarm | SET |  |  |  |  |  |  |  |  |  | ✓ |
| list_artifacts | swarm | swarm | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| read_artifact | swarm | swarm | SET |  |  |  |  |  |  |  |  |  |  |
| spawn_agents | swarm | swarm | SET |  |  |  |  |  |  |  |  |  | ✓ |
| store_artifact | swarm | swarm | SET |  |  |  |  |  |  |  |  |  |  |
| todo_write | task | todo | EXACT |  |  |  |  |  |  |  |  |  | ✓ |
| execute_tools | tools | tool_gateway | EXACT |  |  |  |  |  |  |  |  |  | ? |
| search_tools | tools | tool_gateway | EXACT |  | ✓ |  |  |  |  |  |  |  | ? |
| edit_slides | video | produce | SET |  |  |  |  |  |  |  |  |  | ? |
| motion_video_check | video | motion_video | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| motion_video_concat | video | motion_video | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| motion_video_env_check | video | motion_video | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| motion_video_mux | video | motion_video | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| motion_video_narrate | video | motion_video | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| motion_video_probe | video | motion_video | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| motion_video_render | video | motion_video | SET | ✓ |  |  | ✓ |  |  |  |  |  | ✓ |
| motion_video_storyboard_check | video | motion_video | SET |  | ✓ |  |  |  |  |  |  |  | ✓ |
| produce_report | video | produce | SET |  |  |  |  |  |  |  |  |  | ? |
| produce_research | video | produce | SET |  |  |  |  |  |  |  |  |  | ? |
| produce_slides | video | produce | SET |  |  |  |  |  |  |  |  |  | ? |
| produce_video | video | produce | SET |  |  |  |  |  |  |  |  |  | ? |

## Plugin tools (diagnostic — NOT pinned by --check)

_No third-party plugin tools loaded in this environment._
