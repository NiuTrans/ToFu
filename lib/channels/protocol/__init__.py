"""The channel protocol between the channel core and chat-app adapters (docs/CHANNEL_PROTOCOL.md).

Modules:
  contract — protocol version, methods, health codes, validation, in-repository adapter descriptors
  codec    — thread keys, external IDs, envelopes <-> core shapes, receipts and health <-> legacy vocabulary
  adapter  — the adapter-side base (``ProtocolAdapter``) and the core link it calls
  host     — the core-side endpoint of one adapter session (``AdapterHost``) and the in-process binding
  stdio    — the JSON-RPC 2.0 stdio binding for adapters in another process or runtime

Authority: ``contracts/channel_protocol_v1.schema.json``. This package root deliberately
imports nothing (no import-time work).
"""
