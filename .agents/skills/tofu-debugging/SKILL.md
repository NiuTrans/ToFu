---
name: tofu-debugging
description: Debug a Tofu conversation or task by conversation ID — inspect the sidecar transcript, storage lineage, and app logs with debug/inspect_conversation.py before touching the database.
---

# Debugging Tofu conversations

For a conversation ID, the first command is always:

```bash
python3 debug/inspect_conversation.py <conversation_id>
```

Do not hand-query SQLite or guess table names. With no source flags the
inspector follows the configured storage backend: SQLite deployments resolve
the live Sidecar/fastpath authority; `tofudb` deployments auto-discover the
running application's loopback API (configured `PORT`, default 15000) and
read the transcript through the v4 conversation endpoints, exactly like the
service renders it. Storage facts and flags are documented in the root
`AGENTS.md` and `docs/STORAGE.md`.

This package is instruction-only: it has no resource files. Paths named here
(`debug/inspect_conversation.py`, `docs/STORAGE.md`, logs under `logs/`) are
workspace files — open them with ordinary file tools such as read_files and
grep_search, not read_skill_resource.
