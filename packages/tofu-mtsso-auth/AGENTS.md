# Official SSO adapter

This package is the shared MCP adapter for `@mtfe/mtsso-auth-official`.
The official CLI owns authentication; never reimplement its grants here.
Only structured public errors leave the adapter. Tickets remain in process
memory, scoped by explicit owner/environment/audience, and clear at shutdown.
Run `pytest packages/tofu-mtsso-auth/tests` for CLI semantics and lifecycle.
