# `.vscode/` — shared workspace settings

The files in this directory are **committed on purpose** so every collaborator
(in the VS Code desktop app, in code-server / web-VSCode, or in GitHub
Codespaces) gets the same editor behaviour out of the box.

| File | Purpose | Committed? |
|---|---|---|
| `settings.json` | Workspace settings — mainly `files.watcherExclude` / `search.exclude` to stop code-server's `fileWatcher` from OOMing the host by recursing into `data/pgdata/`, `logs/`, FUSE mounts, etc. | ✅ yes |
| `extensions.json` | Recommended extensions prompt on first open (Python, Ruff, …). | ✅ yes |
| `launch.json` / `tasks.json` / `*.code-workspace` | Personal debug / task configs. | ❌ gitignored |

## Why we need `files.watcherExclude`

On a shared machine with a FUSE-mounted repo we observed **36 code-server
`fileWatcher` worker processes consuming 201 GB RSS** — one of them had grown
to 13 GB over 27 hours. The box ran out of memory and started killing tasks.

The root cause is that VS Code's default watcher descends into every
subdirectory of the workspace, including `data/pgdata/` (Postgres page files),
`logs/` (daily-rotated), `uploads/`, `.git/objects/`, and so on. On FUSE these
`stat()` storms are extremely expensive and the watcher can leak.

The `files.watcherExclude` block in `settings.json` prunes the watch tree so
this can't happen again. Keep it in sync with:
- `.gitignore` (new ignored dirs should usually also be excluded from watching)
- `CLAUDE.md` §1 (directory map) and §9 (cross-DC notes)

## If memory blows up anyway

```bash
# See what's actually eating RAM
ps -eo pid,rss,cmd --sort=-rss | head -20

# Count / kill runaway watchers (they respawn, smaller, with these settings)
pgrep -af 'bootstrap-fork --type=fileWatcher'
pkill -f  'bootstrap-fork --type=fileWatcher'

# Then reload the VS Code window (Ctrl-Shift-P → "Developer: Reload Window")
```
