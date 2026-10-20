# Changelog

## 0.2.1

- Recover one transient worker startup refusal before entering an independent
  read. Keep exact query and repository-handle ownership, report persistent
  per-query failures and join accepted work. The shared worker kernel never
  replays entered tools, trades, initializers or risk/approval decisions.
