# Official SSO adapter

This package is the shared MCP adapter for `@mtfe/mtsso-auth-official`.
The official CLI/API owns grants; the installed DataFE SDK owns shared-session
login/refresh. Never reimplement grants here or use its implicit browser fallback.
`README.md` owns provider/source/identity rules. Only structured public errors
leave the adapter. Tickets remain in process memory, scoped by explicit
owner/environment/audience/source, and clear at shutdown; only
`python -m tofu_mtsso_auth exec` hands a ticket on, into the environment of the
command that replaces the process. Run
`pytest packages/tofu-mtsso-auth/tests` for CLI, offline Node bridge and lifecycle.
