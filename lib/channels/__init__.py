"""Channels: external conversations as event sources and reply targets.

Owner document: docs/CHANNELS.md (decisions C1–C8). Wire/tool authority:
contracts/channels_v1.schema.json.

Modules:
  model     — ThreadKey, ChannelEvent, OutboxItem and contract vocabularies
  store     — ChannelStore(owner): owner-scoped durable state (record CAS)
  policy    — deterministic modes, reply policies, event classification
  ingress   — ingest / ingest_batch: dedup, policy, routing, watermarks
  delivery  — deliver / deliver_command / observe_router via the turn command
  outbox    — enqueue, approve, discard, execute, echo confirmation
  service   — owner-facing views/commands shared by HTTP routes and tools
  tools     — model tools and their authority boundary
  autoreply — Global Router answers return to their command-chat thread
  dx/       — 大象 adapter: bridge.mjs, adapter.py, supervisor.py
  github/   — GitHub adapter: client, normalize, adapter (poller), account, supervisor

This package root deliberately imports nothing (no import-time work).
"""
