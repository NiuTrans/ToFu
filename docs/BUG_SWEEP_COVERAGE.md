# Bug-Sweep Coverage Ledger (waves 1–5)

Reconciled at commit `6c7d17a65933e66a026ab11a185ba5dbd5ac3860` — inventory regenerated from the live tree at closeout.

Machine-checkable per-file ledger for the full-repo bug sweep. Regenerate inventory with `os.walk` (prune dot-dirs to dodge the `lib/.project_sessions` FUSE trap; `find(1)` times out on this mount).

## Totals (reconciled to live tree)

| scope | files |
|---|---|
| root_assets | 1 |
| root_py | 5 |
| routes_py | 71 |
| lib_py | 832 |
| static_js | 141 |
| static_html | 3 |
| static_css | 4 |
| **TOTAL** | **1057** |

## Wave tallies

| verdict bucket | files |
|---|---|
| EXEMPT | 7 |
| wave-1 | 372 |
| wave-2 | 135 |
| wave-3 | 390 |
| wave-4 | 149 |
| wave-5 | 4 |

## Out-of-scope declaration

These directories are deliberately OUTSIDE the sweep surface. Each row states the directory, its file count, and the reason — so "scanned clean" vs "explicitly exempt" vs "forgotten" is never ambiguous.

| dir | files | .py | reason |
|---|---|---|---|
| debug/ | 192 | 141 | EXEMPT — standalone dev/benchmark/audit scripts. .gitignore:66 `/debug/` + export.py:406 explicitly strip the whole dir from opensource exports ("never imported by the app"). Quality gate: run manually by a developer, never imported by server code — a bug here cannot corrupt production. The 5 historically git-tracked scripts were moved to index-only per pt_229606ca. |
| tests/ | 1010 | 980 | EXEMPT — the pytest suite itself. Quality gate: it IS the gate (8500+ tests, collect-only + per-suite runs in CI/make test-unit); test bugs surface as red bars, not production failures. Sweeps of test-code correctness happen via the gate, not this ledger. |
| docs/ | 91 | 0 | EXEMPT — documentation assets (markdown/html diagrams). Not executable code. |
| logs/ | 76 | 0 | EXEMPT — runtime log output (app/access/error/audit/vendor). Not source code. |
| data/ | 168841 | 41680 | EXEMPT — runtime data (DB files, conversations, memory, swarm logs, codelab workspaces, file-history snapshots). Count excludes the heavy runtime subtrees: data/file-history, data/pg_backups, data/pgdata, data/swarm, data/wal_archive. Not source code. |

## Per-file ledger

| file | swept |
|---|---|
| index.html | wave-2 (fe_e structural) + wave-2/wave-3 §3.4 batches |
| bootstrap.py | wave-4 (self: §2.2 grep-level pass — launcher script, stdout is the designed channel pre-logging) |
| export.py | wave-1 (be_a) |
| healthcheck.py | wave-4 (self: §2.2 grep-level pass — launcher script, stdout is the designed channel pre-logging) |
| server.py | wave-1 (be_a) |
| supervisor.py | wave-4 (self: §2.2 grep-level pass — launcher script, stdout is the designed channel pre-logging) |
| routes/__init__.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/_task_routes.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_docs.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/__init__.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/agent_run.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/agents.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/artifacts.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/audio.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/auth.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/auth_mode.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/auth_sources.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/billing.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/browser.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/capabilities.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/chat.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/chat_direct.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/common.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/config.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/conversations.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/daily_report.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/desktop.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/endpoint.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/folders.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/keys.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/logs.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/mcp.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/memory.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/oauth.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/optimizer.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/orchestrations.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/paper.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/paper_folders.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/project.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/providers.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/scheduler.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/skills.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/swarm.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/tasks.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/translate.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/update.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/uploads.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/usage.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/users.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/api_v1/webhooks.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/artifacts.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/browser.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_helpers.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_human_io.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_poll_abort.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_queue.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_side_effects.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_state.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_task_start.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/chat_tool_state.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/common.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/compat_anthropic.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/compat_openai.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/config.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/conversations.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/conversations_compaction.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/conversations_search.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/desktop.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/legacy_redirects.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/metrics.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/oauth.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/paper.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/plugin_registry.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/push.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/translate.py | wave-1 (be_a top-level / be_b api_v1) |
| routes/upload.py | wave-1 (be_a top-level / be_b api_v1) |
| lib/__init__.py | wave-3 (w3_d top-level sweep) |
| lib/_pkg_utils.py | wave-3 (w3_d top-level sweep) |
| lib/agent_artifacts.py | wave-3 (w3_d top-level sweep) |
| lib/agent_core/__init__.py | wave-1 |
| lib/agent_core/activity.py | wave-1 |
| lib/agent_core/admission.py | wave-1 |
| lib/agent_core/affinity.py | wave-1 |
| lib/agent_core/events.py | wave-1 |
| lib/agent_core/personal_scope.py | wave-1 |
| lib/agent_core/principal.py | wave-1 |
| lib/agent_core/profiles.py | wave-1 |
| lib/agent_core/push.py | wave-1 |
| lib/agent_core/push_bus.py | wave-1 |
| lib/agent_core/sse_limit.py | wave-1 |
| lib/agent_core/store.py | wave-1 |
| lib/agent_core/task_runtime.py | wave-1 |
| lib/agent_core_manifest.py | wave-3 (w3_d top-level sweep) |
| lib/agent_inbox/__init__.py | wave-3 |
| lib/agent_inbox/_format.py | wave-3 |
| lib/agent_inbox/_queue.py | wave-3 |
| lib/agent_inbox/_state.py | wave-3 |
| lib/agent_loop.py | wave-1 |
| lib/agent_options.py | wave-3 (w3_d top-level sweep) |
| lib/agent_verdict/__init__.py | wave-3 |
| lib/agent_verdict/_config.py | wave-3 |
| lib/agent_verdict/_handoff.py | wave-3 |
| lib/agent_verdict/_rounds.py | wave-3 |
| lib/agent_verdict/_stuck.py | wave-3 |
| lib/agent_verdict/_verdict.py | wave-3 |
| lib/api_keys/__init__.py | wave-3 |
| lib/api_keys/_context.py | wave-3 |
| lib/api_keys/_crud.py | wave-3 |
| lib/api_keys/_firstrun.py | wave-3 |
| lib/api_keys/_store.py | wave-3 |
| lib/api_keys/_validate.py | wave-3 |
| lib/api_response.py | wave-1 |
| lib/artifacts/__init__.py | wave-3 |
| lib/artifacts/core.py | wave-3 |
| lib/artifacts/events.py | wave-3 |
| lib/artifacts/pdf_export.py | wave-3 |
| lib/artifacts/scanner.py | wave-3 |
| lib/attachments.py | wave-3 (w3_d top-level sweep) |
| lib/auth_mode.py | wave-3 (w3_d top-level sweep) |
| lib/auth_sources.py | wave-3 (w3_d top-level sweep) |
| lib/billing/__init__.py | wave-1 |
| lib/billing/cost.py | wave-1 |
| lib/billing/janitor.py | wave-1 |
| lib/billing/ledger.py | wave-1 |
| lib/billing/payments/__init__.py | wave-1 |
| lib/billing/payments/_common.py | wave-1 |
| lib/billing/payments/alipay.py | wave-1 |
| lib/billing/payments/stripe.py | wave-1 |
| lib/billing/pricing.py | wave-1 |
| lib/billing/request_flow.py | wave-1 |
| lib/billing/users.py | wave-1 |
| lib/billing/wallet.py | wave-1 |
| lib/billing/wallet_janitor.py | wave-1 |
| lib/boot_identity.py | wave-3 (w3_d top-level sweep) |
| lib/branch_meta.py | wave-3 (w3_d top-level sweep) |
| lib/browser/__init__.py | wave-3 |
| lib/browser/advanced.py | wave-3 |
| lib/browser/dispatch.py | wave-3 |
| lib/browser/display.py | wave-3 |
| lib/browser/fetch.py | wave-3 |
| lib/browser/handlers/__init__.py | wave-3 |
| lib/browser/handlers/_capture.py | wave-3 |
| lib/browser/handlers/_interact.py | wave-3 |
| lib/browser/handlers/_page.py | wave-3 |
| lib/browser/handlers/_tabs.py | wave-3 |
| lib/browser/queue/__init__.py | wave-3 |
| lib/browser/queue/_dispatch.py | wave-3 |
| lib/browser/queue/_registry.py | wave-3 |
| lib/browser/queue/_state.py | wave-3 |
| lib/byo_egress.py | wave-3 (w3_d top-level sweep) |
| lib/byo_providers.py | wave-3 (w3_d top-level sweep) |
| lib/byo_resolve.py | wave-3 (w3_d top-level sweep) |
| lib/cgroup_guard.py | wave-3 (w3_d top-level sweep) |
| lib/chat/__init__.py | wave-3 |
| lib/chat/messages.py | wave-3 |
| lib/chat/persistence.py | wave-3 |
| lib/chat/turn_builder.py | wave-3 |
| lib/chat_dispatch.py | wave-3 (w3_d top-level sweep) |
| lib/code_server_excludes.py | wave-3 (w3_d top-level sweep) |
| lib/compat/__init__.py | wave-3 |
| lib/compat/_common.py | wave-3 |
| lib/compat/_platform.py | wave-3 |
| lib/compat/anthropic.py | wave-3 |
| lib/compat/openai.py | wave-3 |
| lib/config_dir.py | wave-3 (w3_d top-level sweep) |
| lib/context_limits/__init__.py | wave-3 |
| lib/context_limits/_learn.py | wave-3 |
| lib/context_limits/_lookup.py | wave-3 |
| lib/context_limits/_store.py | wave-3 |
| lib/conv_config/__init__.py | wave-3 |
| lib/conv_config/_flow.py | wave-3 |
| lib/conv_config/_legacy.py | wave-3 |
| lib/conv_config/_resolve.py | wave-3 |
| lib/conv_config/_translate.py | wave-3 |
| lib/conv_config/_util.py | wave-3 |
| lib/conv_ref/__init__.py | wave-3 |
| lib/conv_ref/_detail.py | wave-3 |
| lib/conv_ref/_query.py | wave-3 |
| lib/conv_ref/_tool.py | wave-3 |
| lib/conversations/__init__.py | wave-1 |
| lib/conversations/meta_cache.py | wave-1 |
| lib/conversations/project_board.py | wave-1 |
| lib/conversations/project_brain_influence.py | wave-1 |
| lib/conversations/project_brain_summary.py | wave-1 |
| lib/conversations/project_charter.py | wave-1 |
| lib/conversations/project_dispatch.py | wave-1 |
| lib/conversations/project_feed.py | wave-1 |
| lib/conversations/project_peer.py | wave-1 |
| lib/conversations/project_status.py | wave-1 |
| lib/conversations/project_summary.py | wave-1 |
| lib/conversations/project_watch.py | wave-1 |
| lib/conversations/reconcile.py | wave-1 |
| lib/conversations/search_index.py | wave-1 |
| lib/conversations/segments_backfill.py | wave-1 |
| lib/conversations/settings_store.py | wave-1 |
| lib/conversations/title_gen.py | wave-1 |
| lib/conversations/turn_initiation.py | wave-1 |
| lib/conversations/turn_settlement.py | wave-1 |
| lib/conversations/vu_translate_backfill.py | wave-1 |
| lib/cost.py | wave-3 (w3_d top-level sweep) |
| lib/cost_estimator.py | wave-3 (w3_d top-level sweep) |
| lib/cross_dc/__init__.py | wave-3 |
| lib/cross_dc/_probe.py | wave-3 |
| lib/cross_dc/_state.py | wave-3 |
| lib/css_bundler.py | wave-3 (w3_d top-level sweep) |
| lib/daily_report/__init__.py | wave-3 |
| lib/daily_report/conversations.py | wave-3 |
| lib/daily_report/cost.py | wave-3 |
| lib/daily_report/generator.py | wave-3 |
| lib/daily_report/llm.py | wave-3 |
| lib/daily_report/prompts.py | wave-3 |
| lib/daily_report/scheduler.py | wave-3 |
| lib/daily_report/storage.py | wave-3 |
| lib/daily_report/todos.py | wave-3 |
| lib/database/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_bootstrap/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_bootstrap/_config.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_bootstrap/_database.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_bootstrap/_orchestrate.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_bootstrap/_process.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_bootstrap/_verify.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_core.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_core_schema/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_core_schema/_ddl.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_core_schema/_helpers.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_core_schema/_tables.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_orphan_heal.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_backup/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_backup/_basebackup.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_backup/_dump.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_backup/_restore.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_backup/_selfheal.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_backup/_shims.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_binaries.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_flock.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_heartbeat.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_hostid.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_identity.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_lock.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_ownership/_ownership.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_pg_seed.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_pg/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_pg/_chat.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_pg/_init.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_pg/_meta.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_pg/_selfheal.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_pg/_system.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_sqlite/__init__.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_sqlite/_chat.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_sqlite/_meta.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_sqlite/_selfheal.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_schema_sqlite/_system.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_sql_translate.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/_wrappers.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/aio.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/db_paths.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/messages_rows.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/pg_admin.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/schema_registry.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/database/wal_archive.py | wave-1 (be_f named skim) + wave-4 (w4_d full) |
| lib/desktop/__init__.py | wave-3 |
| lib/desktop/bridge.py | wave-3 |
| lib/desktop_agent/__init__.py | wave-4 (w4_e) |
| lib/desktop_agent/__main__.py | wave-4 (w4_e) |
| lib/desktop_agent/_dispatch.py | wave-4 (w4_e) |
| lib/desktop_agent/_exec.py | wave-4 (w4_e) |
| lib/desktop_agent/_files.py | wave-4 (w4_e) |
| lib/desktop_agent/_gui.py | wave-4 (w4_e) |
| lib/desktop_agent/_permissions.py | wave-4 (w4_e) |
| lib/desktop_agent/_run.py | wave-4 (w4_e) |
| lib/desktop_agent/_scaling.py | wave-4 (w4_e) |
| lib/desktop_tools.py | wave-3 (w3_d top-level sweep) |
| lib/dispatch_stats.py | wave-3 (w3_d top-level sweep) |
| lib/doc_parser/__init__.py | wave-3 |
| lib/doc_parser/_dispatch.py | wave-3 |
| lib/doc_parser/_legacy.py | wave-3 |
| lib/doc_parser/_office.py | wave-3 |
| lib/doc_parser/_plain.py | wave-3 |
| lib/embeddings.py | wave-3 (w3_d top-level sweep) |
| lib/env_compat.py | wave-3 (w3_d top-level sweep) |
| lib/env_health.py | wave-3 (w3_d top-level sweep) |
| lib/error_envelope/__init__.py | wave-3 |
| lib/error_envelope/_build.py | wave-3 |
| lib/error_envelope/_classify.py | wave-3 |
| lib/error_envelope/_constants.py | wave-3 |
| lib/error_envelope/_serde.py | wave-3 |
| lib/error_fingerprint.py | wave-3 (w3_d top-level sweep) |
| lib/feature_registry.py | wave-3 (w3_d top-level sweep) |
| lib/features_store.py | wave-3 (w3_d top-level sweep) |
| lib/feishu/__init__.py | wave-3 |
| lib/feishu/_state.py | wave-3 |
| lib/feishu/commands.py | wave-3 |
| lib/feishu/conversation.py | wave-3 |
| lib/feishu/events.py | wave-3 |
| lib/feishu/messaging.py | wave-3 |
| lib/feishu/pipeline.py | wave-3 |
| lib/feishu/startup.py | wave-3 |
| lib/file_history/__init__.py | wave-3 |
| lib/file_history/api.py | wave-3 |
| lib/file_history/store.py | wave-3 |
| lib/file_reader/__init__.py | wave-3 |
| lib/file_reader/_docs.py | wave-3 |
| lib/file_reader/_image.py | wave-3 |
| lib/file_reader/_router.py | wave-3 |
| lib/fs_keepalive.py | wave-3 (w3_d top-level sweep) |
| lib/http_client.py | wave-1 |
| lib/idempotency.py | wave-1 |
| lib/ids.py | wave-3 (w3_d top-level sweep) |
| lib/image_gen/__init__.py | wave-3 |
| lib/image_gen/_chat.py | wave-3 |
| lib/image_gen/_errors.py | wave-3 |
| lib/image_gen/_gemini.py | wave-3 |
| lib/image_gen/_generate.py | wave-3 |
| lib/image_gen/_openai.py | wave-3 |
| lib/image_gen/_slots.py | wave-3 |
| lib/js_bundler.py | wave-3 (w3_d top-level sweep) |
| lib/json_store.py | wave-1 |
| lib/key_stats/__init__.py | wave-3 |
| lib/key_stats/_enable.py | wave-3 |
| lib/key_stats/_query.py | wave-3 |
| lib/key_stats/_record.py | wave-3 |
| lib/key_stats/_state.py | wave-3 |
| lib/lang_correct.py | wave-3 (w3_d top-level sweep) |
| lib/llm/__init__.py | wave-1 |
| lib/llm/_sse_core.py | wave-1 |
| lib/llm/_transport.py | wave-1 |
| lib/llm/anthropic_outbound/__init__.py | wave-1 |
| lib/llm/anthropic_outbound/_from_anthropic.py | wave-1 |
| lib/llm/anthropic_outbound/_sse.py | wave-1 |
| lib/llm/anthropic_outbound/_to_anthropic.py | wave-1 |
| lib/llm/anthropic_outbound/_url.py | wave-1 |
| lib/llm/astream.py | wave-1 |
| lib/llm/body/__init__.py | wave-1 |
| lib/llm/body/_build.py | wave-1 |
| lib/llm/body/_canonical_wire.py | wave-1 |
| lib/llm/body/_clamp.py | wave-1 |
| lib/llm/body/_images.py | wave-1 |
| lib/llm/body/_model_tweaks.py | wave-1 |
| lib/llm/cache.py | wave-1 |
| lib/llm/chat.py | wave-1 |
| lib/llm/diagnostics.py | wave-1 |
| lib/llm/json_extract.py | wave-1 |
| lib/llm/stream.py | wave-1 |
| lib/llm_dispatch/__init__.py | wave-4 (w4_e) |
| lib/llm_dispatch/api.py | wave-1 (be_d deep) |
| lib/llm_dispatch/big_prefix_gate.py | wave-4 (w4_e) |
| lib/llm_dispatch/cache_settle.py | wave-4 (w4_e) |
| lib/llm_dispatch/config/__init__.py | wave-4 (w4_e) |
| lib/llm_dispatch/config/_aliases.py | wave-4 (w4_e) |
| lib/llm_dispatch/config/_pricing.py | wave-4 (w4_e) |
| lib/llm_dispatch/config/_slots.py | wave-4 (w4_e) |
| lib/llm_dispatch/conv_affinity.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/__init__.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_balance.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_brand.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_capabilities.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_discover.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_probe.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_thinking.py | wave-4 (w4_e) |
| lib/llm_dispatch/discovery/_url.py | wave-4 (w4_e) |
| lib/llm_dispatch/dispatcher.py | wave-1 (be_d deep) |
| lib/llm_dispatch/ephemeral.py | wave-4 (w4_e) |
| lib/llm_dispatch/factory.py | wave-1 (be_d deep) |
| lib/llm_dispatch/health_local.py | wave-4 (w4_e) |
| lib/llm_dispatch/provider_pin.py | wave-4 (w4_e) |
| lib/llm_dispatch/provider_registry.py | wave-4 (w4_e) |
| lib/llm_dispatch/retry_i18n.py | wave-4 (w4_e) |
| lib/llm_dispatch/slot.py | wave-1 (be_d deep) |
| lib/llm_error_format.py | wave-3 (w3_d top-level sweep) |
| lib/llm_errors.py | wave-1 |
| lib/llm_json.py | wave-3 (w3_d top-level sweep) |
| lib/llm_sanitize/__init__.py | wave-1 |
| lib/llm_sanitize/_fields.py | wave-1 |
| lib/llm_sanitize/_gateway.py | wave-1 |
| lib/llm_sanitize/_messages.py | wave-1 |
| lib/llm_sanitize/_toolcalls.py | wave-1 |
| lib/log.py | wave-1 (be_f skim — logging infra itself) |
| lib/log_clean/__init__.py | wave-3 |
| lib/log_clean/_collapse.py | wave-3 |
| lib/log_clean/_detect.py | wave-3 |
| lib/log_clean/_helpers.py | wave-3 |
| lib/log_clean/_patterns.py | wave-3 |
| lib/log_clean/_types.py | wave-3 |
| lib/mcp/__init__.py | wave-1 |
| lib/mcp/client/__init__.py | wave-1 |
| lib/mcp/client/_bridge.py | wave-1 |
| lib/mcp/client/_coerce.py | wave-1 |
| lib/mcp/client/_errors.py | wave-1 |
| lib/mcp/client/_install.py | wave-1 |
| lib/mcp/client/_state.py | wave-1 |
| lib/mcp/client/_vendor.py | wave-1 |
| lib/mcp/config.py | wave-1 |
| lib/mcp/health_probe.py | wave-1 |
| lib/mcp/project_names.py | wave-1 |
| lib/mcp/registry.py | wave-1 |
| lib/mcp/types.py | wave-1 |
| lib/mcp/vendored.py | wave-1 |
| lib/memory/__init__.py | wave-1 |
| lib/memory/injection.py | wave-1 |
| lib/memory/prefetch/__init__.py | wave-1 |
| lib/memory/prefetch/_config.py | wave-1 |
| lib/memory/prefetch/_inject.py | wave-1 |
| lib/memory/prefetch/_query.py | wave-1 |
| lib/memory/prefetch/_run.py | wave-1 |
| lib/memory/prefetch/_shortlist.py | wave-1 |
| lib/memory/profile_consolidate.py | wave-1 |
| lib/memory/relevance/__init__.py | wave-1 |
| lib/memory/relevance/_score.py | wave-1 |
| lib/memory/relevance/_search.py | wave-1 |
| lib/memory/relevance/_tokenize.py | wave-1 |
| lib/memory/storage/__init__.py | wave-1 |
| lib/memory/storage/_crud.py | wave-1 |
| lib/memory/storage/_dirs.py | wave-1 |
| lib/memory/storage/_files.py | wave-1 |
| lib/memory/storage/_frontmatter.py | wave-1 |
| lib/memory/tools.py | wave-1 |
| lib/memory/user_profile/__init__.py | wave-1 |
| lib/memory/user_profile/_io.py | wave-1 |
| lib/memory/user_profile/_mutate.py | wave-1 |
| lib/memory/user_profile/_paths.py | wave-1 |
| lib/memory/user_profile/_pending.py | wave-1 |
| lib/memory/user_profile/_render.py | wave-1 |
| lib/message_queue.py | wave-1 |
| lib/model_info/__init__.py | wave-3 |
| lib/model_info/_capabilities.py | wave-3 |
| lib/model_info/_family.py | wave-3 |
| lib/model_info/_limits.py | wave-3 |
| lib/model_info/_max_output.py | wave-3 |
| lib/model_info/capability_taxonomy.py | wave-3 |
| lib/mt_provider/__init__.py | wave-3 |
| lib/mt_provider/_config.py | wave-3 |
| lib/mt_provider/_markdown.py | wave-3 |
| lib/mt_provider/_niutrans.py | wave-3 |
| lib/mt_provider/_translate.py | wave-3 |
| lib/oauth/__init__.py | wave-3 |
| lib/oauth/claude.py | wave-3 |
| lib/oauth/codex.py | wave-3 |
| lib/oauth/manager/__init__.py | wave-3 |
| lib/oauth/manager/_exchange.py | wave-3 |
| lib/oauth/manager/_flow.py | wave-3 |
| lib/oauth/manager/_relay.py | wave-3 |
| lib/oauth/manager/_state.py | wave-3 |
| lib/oauth/outbound.py | wave-3 |
| lib/oauth/pkce.py | wave-3 |
| lib/oauth/token_store.py | wave-3 |
| lib/onnx_thread_guard.py | wave-3 (w3_d top-level sweep) |
| lib/openapi/__init__.py | wave-3 |
| lib/openapi/_docs.py | wave-3 |
| lib/openapi/_meta.py | wave-3 |
| lib/openapi/_paths.py | wave-3 |
| lib/openapi/_schema.py | wave-3 |
| lib/openapi/_spec.py | wave-3 |
| lib/optimizer/__init__.py | wave-3 |
| lib/optimizer/actions/__init__.py | wave-3 |
| lib/optimizer/actions/block_search_domain.py | wave-3 |
| lib/optimizer/analyzer/__init__.py | wave-3 |
| lib/optimizer/analyzer/_audit.py | wave-3 |
| lib/optimizer/analyzer/_domains.py | wave-3 |
| lib/optimizer/analyzer/_issues.py | wave-3 |
| lib/optimizer/analyzer/_logs.py | wave-3 |
| lib/optimizer/analyzer/_metrics.py | wave-3 |
| lib/optimizer/analyzer/_model.py | wave-3 |
| lib/optimizer/analyzer/_signals.py | wave-3 |
| lib/optimizer/applier.py | wave-3 |
| lib/optimizer/orchestrator.py | wave-3 |
| lib/optimizer/proposer.py | wave-3 |
| lib/optimizer/storage.py | wave-3 |
| lib/orchestration/__init__.py | wave-4 (w4_e) |
| lib/orchestration/_build.py | wave-4 (w4_e) |
| lib/orchestration/_io.py | wave-4 (w4_e) |
| lib/orchestration/_layout.py | wave-4 (w4_e) |
| lib/orchestration/_roles.py | wave-4 (w4_e) |
| lib/orchestration/_validate.py | wave-4 (w4_e) |
| lib/orchestration_composer.py | wave-1 |
| lib/orchestration_endpoint_adapter.py | wave-1 |
| lib/orchestration_endpoint_runner.py | wave-1 |
| lib/orchestration_engine.py | wave-1 |
| lib/orchestration_runs.py | wave-1 |
| lib/paper/__init__.py | wave-3 |
| lib/paper/arxiv.py | wave-3 |
| lib/paper/citation_audit.py | wave-3 |
| lib/paper/hashing.py | wave-3 |
| lib/paper/images/__init__.py | wave-3 |
| lib/paper/images/_extract.py | wave-3 |
| lib/paper/images/_inject.py | wave-3 |
| lib/paper/images/_title.py | wave-3 |
| lib/paper/injection_guard.py | wave-3 |
| lib/paper/insight_engine/__init__.py | wave-3 |
| lib/paper/insight_engine/_config.py | wave-3 |
| lib/paper/insight_engine/_context.py | wave-3 |
| lib/paper/insight_engine/_grounding.py | wave-3 |
| lib/paper/insight_engine/_render.py | wave-3 |
| lib/paper/insight_engine/_rubric.py | wave-3 |
| lib/paper/insight_engine/_run.py | wave-3 |
| lib/paper/insight_engine/_synthesize.py | wave-3 |
| lib/paper/insight_prompts.py | wave-3 |
| lib/paper/library.py | wave-3 |
| lib/paper/llm_stream.py | wave-3 |
| lib/paper/openreview_autofill.py | wave-3 |
| lib/paper/prompts.py | wave-3 |
| lib/paper/qa_context.py | wave-3 |
| lib/paper/qa_engine.py | wave-3 |
| lib/paper/qa_runtime.py | wave-3 |
| lib/paper/recommend_engine/__init__.py | wave-3 |
| lib/paper/recommend_engine/_events.py | wave-3 |
| lib/paper/recommend_engine/_ground.py | wave-3 |
| lib/paper/recommend_engine/_research.py | wave-3 |
| lib/paper/recommend_runtime.py | wave-3 |
| lib/paper/recommend_task.py | wave-3 |
| lib/paper/report_engine/__init__.py | wave-3 |
| lib/paper/report_engine/_hooks.py | wave-3 |
| lib/paper/report_engine/_meta.py | wave-3 |
| lib/paper/report_runtime.py | wave-3 |
| lib/paper/review/__init__.py | wave-3 |
| lib/paper/review/_lang.py | wave-3 |
| lib/paper/review/_prompts.py | wave-3 |
| lib/paper/review/_textproc.py | wave-3 |
| lib/paper/terminology_audit/__init__.py | wave-3 |
| lib/paper/terminology_audit/_acronyms.py | wave-3 |
| lib/paper/terminology_audit/_glossary.py | wave-3 |
| lib/paper/terminology_audit/_sections.py | wave-3 |
| lib/paper/terminology_backfill.py | wave-3 |
| lib/paper/tools.py | wave-3 |
| lib/paper/translate_engine.py | wave-3 |
| lib/paper/translate_runtime.py | wave-3 |
| lib/pdf_parser/__init__.py | wave-3 |
| lib/pdf_parser/_common.py | wave-3 |
| lib/pdf_parser/core.py | wave-3 |
| lib/pdf_parser/docling.py | wave-3 |
| lib/pdf_parser/images/__init__.py | wave-3 |
| lib/pdf_parser/images/_detect.py | wave-3 |
| lib/pdf_parser/images/_render.py | wave-3 |
| lib/pdf_parser/images/_resize.py | wave-3 |
| lib/pdf_parser/math.py | wave-3 |
| lib/pdf_parser/pool.py | wave-3 |
| lib/pdf_parser/postprocess.py | wave-3 |
| lib/pdf_parser/text.py | wave-3 |
| lib/pdf_parser/vlm/__init__.py | wave-3 |
| lib/pdf_parser/vlm/_config.py | wave-3 |
| lib/pdf_parser/vlm/_parse.py | wave-3 |
| lib/pdf_parser/vlm/_tasks.py | wave-3 |
| lib/pptx_translator.py | wave-3 (w3_d top-level sweep) |
| lib/presence/__init__.py | wave-3 |
| lib/presence/conflict.py | wave-3 |
| lib/presence/registry.py | wave-3 |
| lib/pricing/__init__.py | wave-3 |
| lib/pricing/_provider.py | wave-3 |
| lib/pricing/_refresh.py | wave-3 |
| lib/pricing/_tables.py | wave-3 |
| lib/project_mod/__init__.py | wave-3 |
| lib/project_mod/abs_path_guard.py | wave-3 |
| lib/project_mod/ansi_normalize.py | wave-3 |
| lib/project_mod/command_analysis.py | wave-3 |
| lib/project_mod/config.py | wave-3 |
| lib/project_mod/gitignore_suggest.py | wave-3 |
| lib/project_mod/indexer.py | wave-3 |
| lib/project_mod/journal.py | wave-3 |
| lib/project_mod/modifications.py | wave-3 |
| lib/project_mod/portable_sandbox.py | wave-3 |
| lib/project_mod/read_tools.py | wave-3 |
| lib/project_mod/run_command.py | wave-3 |
| lib/project_mod/scanner.py | wave-3 |
| lib/project_mod/tools.py | wave-3 |
| lib/project_mod/write_tools/__init__.py | wave-3 |
| lib/project_mod/write_tools/_ops.py | wave-3 |
| lib/project_mod/write_tools/_paths.py | wave-3 |
| lib/project_mod/write_tools/_text.py | wave-3 |
| lib/protocols.py | wave-3 (w3_d top-level sweep) |
| lib/provider_balance.py | wave-3 (w3_d top-level sweep) |
| lib/provider_defaults.py | wave-3 (w3_d top-level sweep) |
| lib/provider_probe.py | wave-3 (w3_d top-level sweep) |
| lib/proxy.py | wave-3 (w3_d top-level sweep) |
| lib/push.py | wave-1 |
| lib/rate_limit_api.py | wave-3 (w3_d top-level sweep) |
| lib/rate_limit_store.py | wave-3 (w3_d top-level sweep) |
| lib/rate_limiter.py | wave-3 (w3_d top-level sweep) |
| lib/relay_config.py | wave-3 (w3_d top-level sweep) |
| lib/request_parser.py | wave-1 |
| lib/runtime_layout.py | wave-3 (w3_d top-level sweep) |
| lib/runtime_paths.py | wave-3 (w3_d top-level sweep) |
| lib/runtime_state_store.py | wave-3 (w3_d top-level sweep) |
| lib/scheduler/__init__.py | wave-1 |
| lib/scheduler/_shared.py | wave-1 |
| lib/scheduler/cron.py | wave-1 |
| lib/scheduler/executor/__init__.py | wave-1 |
| lib/scheduler/executor/_await.py | wave-1 |
| lib/scheduler/executor/_common.py | wave-1 |
| lib/scheduler/executor/_timer.py | wave-1 |
| lib/scheduler/manager.py | wave-1 |
| lib/scheduler/proactive.py | wave-1 |
| lib/scheduler/timer/__init__.py | wave-1 |
| lib/scheduler/timer/_crud.py | wave-1 |
| lib/scheduler/timer/_loop.py | wave-1 |
| lib/scheduler/timer/_poll.py | wave-1 |
| lib/scheduler/timer/_state.py | wave-1 |
| lib/scheduler/tool_defs.py | wave-1 |
| lib/search_bridge.py | wave-3 (w3_d top-level sweep) |
| lib/self_update/__init__.py | wave-3 |
| lib/self_update/_apply.py | wave-3 |
| lib/self_update/_config.py | wave-3 |
| lib/self_update/_git.py | wave-3 |
| lib/self_update/_requirements.py | wave-3 |
| lib/self_update/_status.py | wave-3 |
| lib/self_update/_version.py | wave-3 |
| lib/settings_panels.py | wave-3 (w3_d top-level sweep) |
| lib/shutdown_marker.py | wave-3 (w3_d top-level sweep) |
| lib/skills/__init__.py | wave-3 |
| lib/skills/load.py | wave-3 (renamed from activate.py) |
| lib/skills/catalog.py | wave-3 |
| lib/skills/injection.py | wave-3 |
| lib/skills/installer.py | wave-3 |
| lib/skills/registry.py | wave-3 |
| lib/skills/tools.py | wave-3 |
| lib/swarm/__init__.py | wave-1 |
| lib/swarm/agent.py | wave-1 |
| lib/swarm/artifact_store.py | wave-1 |
| lib/swarm/events.py | wave-1 |
| lib/swarm/integration/__init__.py | wave-1 |
| lib/swarm/integration/_autocontinue.py | wave-1 |
| lib/swarm/integration/_config.py | wave-1 |
| lib/swarm/integration/_logs.py | wave-1 |
| lib/swarm/integration/_rehydrate.py | wave-1 |
| lib/swarm/integration/_state.py | wave-1 |
| lib/swarm/integration/_tools.py | wave-1 |
| lib/swarm/master.py | wave-1 |
| lib/swarm/messages.py | wave-1 |
| lib/swarm/persistence.py | wave-1 |
| lib/swarm/planner.py | wave-1 |
| lib/swarm/protocol.py | wave-1 |
| lib/swarm/rate_limiter.py | wave-1 |
| lib/swarm/registry.py | wave-1 |
| lib/swarm/result_format.py | wave-1 |
| lib/swarm/scheduler.py | wave-1 |
| lib/swarm/snapshot.py | wave-1 |
| lib/swarm/tools.py | wave-1 |
| lib/swarm/types.py | wave-1 |
| lib/task_runtime.py | wave-1 |
| lib/tasks_pkg/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/activity_sink.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/approval.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/attachments.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/auto_translate/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/auto_translate/_assistant.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/auto_translate/_critic.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/autopilot.py | wave-5 read-only audit (orchestrator personally read 2506L, CLEAN; sibling epic owns edits) |
| lib/tasks_pkg/autopilot_markers.py | wave-5 read-only audit (orchestrator personally read 2506L, CLEAN; sibling epic owns edits) |
| lib/tasks_pkg/autopilot_run_lifecycle.py | wave-5 read-only audit (orchestrator personally read 2506L, CLEAN; sibling epic owns edits) |
| lib/tasks_pkg/autopilot_state.py | wave-5 read-only audit (orchestrator personally read 2506L, CLEAN; sibling epic owns edits) |
| lib/tasks_pkg/cache_tracking/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_detect.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_hashing.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_persist.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_prefix.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_roi.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_state.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/_ttl.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/cache_tracking/replay.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/chat_mode.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/commit_round/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/commit_round/_commit.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/commit_round/_derive.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/commit_round/_profile.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_advanced.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_archive.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_budget.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/_assistant.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/_images.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/_interstitial.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/_shared.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/_thinking.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_builtin_steps/_toolresults.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_compaction_usage.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_constants.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_faithful_methods/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_faithful_methods/_hermes.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_faithful_methods/_openclaw.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_faithful_methods/_opencode.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_faithful_methods/_shared.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_faithful_methods/_state.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_layer1.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_layer2/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_layer2/_anchor.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_layer2/_compact.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_layer2/_prompt.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_layer2/_summary.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_manual.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_dedup.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_drop.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_fold.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_prune.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_shared.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_summarize.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_methods/_tail.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_persist/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_persist/_helpers.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_persist/_splitters.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_pipeline.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_reactive/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_reactive/_headtrunc.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_reactive/_measure.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_reactive/_strip.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_steps.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/compaction/_tokens.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/conv_message_builder/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/conv_message_builder/_dedup.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/conv_message_builder/_load.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/conv_message_builder/_toolcalls.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/conv_message_builder/_transform.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/endpoint/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/endpoint/_replan.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/endpoint/_run.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/endpoint/_sync.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/endpoint/_translate.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/endpoint_prompts/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/endpoint_prompts/_critic.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/endpoint_prompts/_planner.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/endpoint_prompts/_worker.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/endpoint_review.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/entry.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/event_fold.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/event_log.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/executor/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/executor/_content_ref.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/executor/_execute.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/executor/_finalize.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/executor/_registry.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/executor/_summary.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/executor_image/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/executor_image/_register.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/executor_image/_resolve.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/executor_image/_save.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/executor_image/_svg.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/executor_image/_thumbnail.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/floor_retry.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/_adapter.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/_read_gate.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/browser.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/code_exec.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/mcp.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/memory.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/misc/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/misc/_agents.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/misc/_brain.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/misc/_human.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/project.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/search/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/search/_core.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/search/_display.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/search/_handlers.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/handlers/skills.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/human_guidance.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/killed_recovery.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/llm_fallback/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/llm_fallback/_call.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/llm_fallback/_retry.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/llm_fallback/_state.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/llm_fallback/_usage.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/manager/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_events.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_maintenance.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_persist.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_recovery.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_registry.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_state.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_stream.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/manager/_sync.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/message_builder/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/message_builder/_prefetch.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/message_builder/_tool_history.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/model_config.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/orchestrator/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_context_inject.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_finalize.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_post_loop.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_prefetch.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_run.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_teardown.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_tool_history.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_turn.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/orchestrator/_vu_startup.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/persist_registry.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/persistence_store.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/_assemble.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/_derive.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/_edit.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/_project.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/_serde.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/segments/_types.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/server_message_store/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/server_message_store/_rebuild.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/server_message_store/_store.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/server_message_store/_truncate.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/stdin_handler.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/stream_handler/__init__.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/stream_handler/_analyse.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/stream_handler/_audit.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/stream_handler/_budget.py | wave-1 (be_c named) + wave-4 (w4_a/b4-b14 deep) |
| lib/tasks_pkg/streaming_tool_executor.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_context/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_context/_inject.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_context/_profile.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_context/_reminders.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_context/_search.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_prompt_cc/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_prompt_cc/_build.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_prompt_cc/_environment.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/system_prompt_cc/_sections.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_approval.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_flags.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_heartbeat.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_labels.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_parse.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_pipeline.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_dispatch/_repair.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_display/__init__.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_display/_dispatch.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_display/_mcp.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_display/_renderers.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_display/_roots.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/tool_hooks.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/turn_retry.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/wire_fingerprint.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/wire_messages.py | wave-4 (w4_c subpackages) |
| lib/tasks_pkg/write_breakdown.py | wave-4 (w4_c subpackages) |
| lib/tests/__init__.py | EXEMPT — in-lib test scaffolding (validate_imports), exercised by pytest itself |
| lib/tests/validate_imports.py | EXEMPT — in-lib test scaffolding (validate_imports), exercised by pytest itself |
| lib/text_lang/__init__.py | wave-3 |
| lib/text_lang/_detect.py | wave-3 |
| lib/text_lang/_fasttext.py | wave-3 |
| lib/text_lang/_policy.py | wave-3 |
| lib/text_lang/_ratios.py | wave-3 |
| lib/timeutil.py | wave-3 (w3_d top-level sweep) |
| lib/token_counter/__init__.py | wave-3 |
| lib/token_counter/anthropic_api.py | wave-3 |
| lib/token_counter/api.py | wave-3 |
| lib/token_counter/base.py | wave-3 |
| lib/token_counter/config.py | wave-3 |
| lib/token_counter/deepseek_counter.py | wave-3 |
| lib/token_counter/gemini_api.py | wave-3 |
| lib/token_counter/heuristic.py | wave-3 |
| lib/token_counter/hf_counter.py | wave-3 |
| lib/token_counter/resolver.py | wave-3 |
| lib/token_counter/tiktoken_counter.py | wave-3 |
| lib/token_counter/usage_cache.py | wave-3 |
| lib/tool_changes.py | wave-3 (w3_d top-level sweep) |
| lib/tool_input_repair/__init__.py | wave-3 |
| lib/tool_input_repair/_classify.py | wave-3 |
| lib/tool_input_repair/_ingest.py | wave-3 |
| lib/tool_input_repair/_rejection.py | wave-3 |
| lib/tool_input_repair/_repair.py | wave-3 |
| lib/tool_input_repair/_salvage.py | wave-3 |
| lib/tool_input_repair/_schema.py | wave-3 |
| lib/tool_input_repair/_transform.py | wave-3 |
| lib/tools/__init__.py | wave-3 |
| lib/tools/browser.py | wave-3 |
| lib/tools/code_exec.py | wave-3 |
| lib/tools/conversation.py | wave-3 |
| lib/tools/human_guidance.py | wave-3 |
| lib/tools/image_edit.py | wave-3 |
| lib/tools/image_gen.py | wave-3 |
| lib/tools/meta.py | wave-3 |
| lib/tools/project.py | wave-3 |
| lib/tools/registry/__init__.py | wave-3 |
| lib/tools/registry/_build.py | wave-3 |
| lib/tools/registry/_latch.py | wave-3 |
| lib/tools/registry/_plugins.py | wave-3 |
| lib/tools/registry/_spec.py | wave-3 |
| lib/tools/search.py | wave-3 |
| lib/tools/todo.py | wave-3 |
| lib/tools/tool_env.py | wave-3 |
| lib/trajectory.py | wave-3 (w3_d top-level sweep) |
| lib/transcription/__init__.py | wave-3 |
| lib/transcription/_audio.py | wave-3 |
| lib/transcription/_config.py | wave-3 |
| lib/transcription/_correct.py | wave-3 |
| lib/transcription/_transcribe.py | wave-3 |
| lib/transcription/_zh.py | wave-3 |
| lib/translate/__init__.py | wave-3 |
| lib/translate/commit.py | wave-3 |
| lib/translate/constants.py | wave-3 |
| lib/translate/dedup.py | wave-3 |
| lib/translate/engine/__init__.py | wave-3 |
| lib/translate/engine/_engine.py | wave-3 |
| lib/translate/engine/_split.py | wave-3 |
| lib/translate/incremental.py | wave-3 |
| lib/translate/inflight.py | wave-3 |
| lib/translate/notranslate.py | wave-3 |
| lib/translate/pptx.py | wave-3 |
| lib/translate/prompt.py | wave-3 |
| lib/translate/runtime/__init__.py | wave-3 |
| lib/translate/runtime/_segments.py | wave-3 |
| lib/translate/runtime/_state.py | wave-3 |
| lib/translate/runtime/_worker.py | wave-3 |
| lib/translate/segment_backfill.py | wave-3 |
| lib/translate/status.py | wave-3 |
| lib/translate_cache.py | wave-3 (w3_d top-level sweep) |
| lib/ttl_cache.py | wave-1 |
| lib/usage_tracker.py | wave-3 (w3_d top-level sweep) |
| lib/utils.py | wave-3 (w3_d top-level sweep) |
| lib/version.py | wave-3 (w3_d top-level sweep) |
| static/js/api.js | wave-2 |
| static/js/artifacts.js | wave-2 |
| static/js/branch.js | wave-2 |
| static/js/branch_stream.js | wave-2 |
| static/js/bundle-005135bc.js | EXEMPT — build artifact (regenerated by lib/js_bundler.py on boot) |
| static/js/compaction-viewer.js | wave-2 |
| static/js/context-bar.js | wave-2 |
| static/js/conv_sync_push.js | wave-2 |
| static/js/conv_view.js | wave-2 |
| static/js/conv_window.js | wave-2 |
| static/js/core.js | wave-2 |
| static/js/core/async_pool.js | wave-2 |
| static/js/core/cache_stats.js | wave-2 |
| static/js/core/conv_state_reducer.js | wave-2 |
| static/js/core/conversations.js | wave-2 |
| static/js/core/cost.js | wave-2 |
| static/js/core/cross_tab_sync.js | wave-2 |
| static/js/core/debug_panel.js | wave-2 |
| static/js/core/dialog.js | wave-2 |
| static/js/core/error_envelope.js | wave-2 |
| static/js/core/escape_html.js | wave-2 |
| static/js/core/folders.js | wave-2 |
| static/js/core/format_size.js | wave-2 |
| static/js/core/health_stream_timer.js | wave-2 |
| static/js/core/icons.js | wave-2 |
| static/js/core/markdown.js | wave-2 |
| static/js/core/model_caps.js | wave-2 |
| static/js/core/safe_html.js | wave-2 |
| static/js/core/sse_reader.js | wave-2 |
| static/js/core/toast.js | wave-2 |
| static/js/core/translate_guard.js | wave-2 |
| static/js/core/translation_model.js | wave-2 |
| static/js/core/turn_settlement.js | wave-2 |
| static/js/core/zip_drop_zone.js | wave-2 |
| static/js/diag_collect.js | wave-2 |
| static/js/export-images.js | wave-2 |
| static/js/feature-fdf9e313.js | EXEMPT — build artifact (regenerated by lib/js_bundler.py on boot) |
| static/js/feature-loader.js | EXEMPT — build artifact (regenerated by lib/js_bundler.py on boot) |
| static/js/i18n.js | wave-3 (w3_e) |
| static/js/idb-cache.js | wave-2 |
| static/js/image-gen-batch.js | wave-2 |
| static/js/image-gen.js | wave-2 |
| static/js/info-rail.js | wave-2 |
| static/js/log-clean.js | wave-2 |
| static/js/main.js | wave-2 |
| static/js/main/main_conv_lifecycle.js | wave-2 |
| static/js/main/main_folders_mobile.js | wave-2 |
| static/js/main/main_init_tasks.js | wave-2 |
| static/js/main/main_input_handling.js | wave-2 |
| static/js/main/main_regen_continue.js | wave-2 |
| static/js/main/main_send_pipeline.js | wave-2 |
| static/js/main/main_toolbar_ui.js | wave-2 |
| static/js/main/main_translating_bubble.js | wave-2 |
| static/js/memory.js | wave-2 |
| static/js/memory_skill_install.js | wave-2 |
| static/js/mobile_panels.js | wave-2 |
| static/js/myday.js | wave-2 |
| static/js/myday_tasks.js | wave-2 |
| static/js/net-latency.js | wave-2 |
| static/js/optimizer.js | wave-2 |
| static/js/orchestration-catalog.js | wave-2 |
| static/js/orchestration.js | wave-2 |
| static/js/paper-reader.js | wave-2 |
| static/js/paper/arxiv.js | wave-2 |
| static/js/paper/babel.js | wave-2 |
| static/js/paper/library.js | wave-2 |
| static/js/paper/pdf_responsive.js | wave-2 |
| static/js/paper/pdf_viewer.js | wave-2 |
| static/js/paper/qa.js | wave-2 |
| static/js/paper/reader_prefs.js | wave-2 |
| static/js/paper/report.js | wave-2 |
| static/js/preferences.js | wave-2 |
| static/js/presence.js | wave-2 |
| static/js/project-brain-i18n.js | wave-3 (w3_e) |
| static/js/project-brain-peers.js | wave-2 |
| static/js/project-brain-status.js | wave-2 |
| static/js/project-brain.js | wave-2 |
| static/js/project.js | wave-2 |
| static/js/push.js | wave-2 |
| static/js/relay-admin.js | wave-2 |
| static/js/scheduler.js | wave-2 |
| static/js/settings.js | wave-3 (w3_e) |
| static/js/settings/auth_sources.js | wave-2 (settings/providers nested) |
| static/js/settings/auto_setup.js | wave-2 (settings/providers nested) |
| static/js/settings/balance.js | wave-2 (settings/providers nested) |
| static/js/settings/branding.js | wave-2 (settings/providers nested) |
| static/js/settings/core_panel.js | wave-2 (settings/providers nested) |
| static/js/settings/key_stats.js | wave-2 (settings/providers nested) |
| static/js/settings/local_endpoints.js | wave-2 (settings/providers nested) |
| static/js/settings/mcp.js | wave-2 (settings/providers nested) |
| static/js/settings/model_edit.js | wave-2 (settings/providers nested) |
| static/js/settings/oauth.js | wave-2 (settings/providers nested) |
| static/js/settings/other_tabs.js | wave-2 (settings/providers nested) |
| static/js/settings/provider_render.js | wave-2 (settings/providers nested) |
| static/js/settings/provider_templates.js | wave-2 (settings/providers nested) |
| static/js/settings/providers/access_matrix.js | wave-2 |
| static/js/settings/save_export.js | wave-2 (settings/providers nested) |
| static/js/settings/speech.js | wave-2 (settings/providers nested) |
| static/js/settings/system_prompt_editor.js | wave-2 (settings/providers nested) |
| static/js/settings/template_actions.js | wave-2 (settings/providers nested) |
| static/js/settings/visibility_defaults.js | wave-2 (settings/providers nested) |
| static/js/skills.js | wave-2 |
| static/js/skills_install.js | wave-2 |
| static/js/task-mode.js | wave-2 |
| static/js/timer.js | wave-2 |
| static/js/tofu-pet.js | wave-3 (w3_e) |
| static/js/tofu-scene.js | wave-3 (w3_e) |
| static/js/translation.js | wave-2 |
| static/js/ui/chat_render.js | wave-2 |
| static/js/ui/conversation_list.js | wave-2 |
| static/js/ui/edit_message.js | wave-2 |
| static/js/ui/finish_info.js | wave-2 |
| static/js/ui/image_fullscreen.js | wave-2 |
| static/js/ui/message_actions.js | wave-2 |
| static/js/ui/popups.js | wave-2 |
| static/js/ui/send_button.js | wave-2 |
| static/js/ui/sse_handlers_io.js | wave-2 |
| static/js/ui/sse_handlers_lifecycle.js | wave-2 |
| static/js/ui/sse_handlers_misc.js | wave-2 |
| static/js/ui/sse_handlers_swarm.js | wave-2 |
| static/js/ui/sse_handlers_tool.js | wave-2 |
| static/js/ui/sse_pipeline.js | wave-2 |
| static/js/ui/sse_poll_fallback.js | wave-2 |
| static/js/ui/stream_lifecycle.js | wave-2 |
| static/js/ui/stream_reducer.js | wave-2 |
| static/js/ui/stream_session.js | wave-2 |
| static/js/ui/streaming_render.js | wave-2 |
| static/js/ui/streaming_swarm_panel.js | wave-2 |
| static/js/ui/streaming_ui.js | wave-2 |
| static/js/ui/swarm_push.js | wave-2 |
| static/js/ui/tool_rounds.js | wave-2 |
| static/js/ui/tool_rounds.js.nc_copy.js | wave-2 |
| static/js/ui/translation_indicator.js | wave-2 |
| static/js/ui/translation_render.js | wave-2 |
| static/js/ui/turn_nav.js | wave-2 |
| static/js/update.js | wave-2 |
| static/js/upload.js | wave-2 |
| static/js/upload_preview.js | wave-2 |
| static/js/voice.js | wave-2 |
| static/js/widgets/chip_input.js | wave-2 |
| static/admin.html | wave-3 (w3_e) + wave-3 fix |
| static/dashboard.html | wave-4 (self: grep sweep + §3.4 glyph fixes) |
| static/login.html | wave-4 (self: grep sweep + §3.4 glyph fixes) |
| static/settings-2901a0b1.css | EXEMPT — hashed bundle artifact |
| static/settings.css | wave-4 (self: brace-balance check) |
| static/styles-592a7d5d.css | EXEMPT — hashed bundle artifact |
| static/styles.css | wave-2 (fe_e structural) + wave-4 (brace re-check) |
