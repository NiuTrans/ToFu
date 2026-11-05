"""GitHub channel adapter package (docs/CHANNELS.md C8).

Modules: ``client`` (REST transport, conditional requests, paging, rate
limits), ``normalize`` (payloads → ``ChannelEvent`` facts), ``adapter`` (one
owner's poll worker and outbox sender), ``account`` (Settings commands and
the vault-held token), ``supervisor`` (start/stop/restart on the scheduler
tick). This package root imports nothing.
"""
