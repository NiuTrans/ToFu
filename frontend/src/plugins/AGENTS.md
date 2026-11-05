# Plugin browser boundary

`ports.ts` declares the public `PluginHostV1` carrier; `page.ts` imports the
page-declared module and awaits its `mount(host)` entry. `../plugin.ts` is the
manifest-owned host entry. Plugins own their views and existing global action
receivers; the host imports no named plugin and publishes no vendor globals.

Entry names and source paths belong to `frontend/authoring-inputs.json`.
The host Jinja helper is registered by `lib/http_transport/plugin_frontend.py`.
Verify typed ports, startup failure/order, prefix-relative asset delivery and
the installed trading wheel in the current native host.
