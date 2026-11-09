"""Bot platform channels (docs/CHANNELS.md C12): 企业微信, QQ and 微信 bots.

Modules:
  ids        — provider IDs ⇄ lowercase thread keys; time-ordered event IDs
  platform   — the transport port: ``Inbound`` messages, ``SessionEnded``
  session    — ``BotSession``: one platform connection, pairing, ingress, outbox
  account    — Settings views and commands, credential custody, pairing codes
  supervisor — start, stop and restart sessions from the scheduler tick

Transports live in ``lib/channels/{wecom,qq,wechat}/``. Every inbound message
goes through ``lib.channels.ingress`` (the shared C2/C7 authority boundary).
This package root deliberately imports nothing (no import-time work).
"""
