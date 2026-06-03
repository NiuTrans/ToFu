# Tofu — Install Guide

You only need this page if the one-command install in the README didn't
work, or if you want to pre-configure something (API key, port,
install directory, …).

For the happy path, just run the command from the README and skip this
file entirely.

---

## Pre-configuring the install (Linux / macOS)

Windows users: just run the `.exe` from the release page — no flags.
The sections below apply only to the Linux/macOS `install.sh`.

`install.sh` accepts the flags below. They write to `.env` and start
the server when finished.

| Flag | Purpose |
|---|---|
| `--api-key sk-xxx` | Pre-configure your LLM API key |
| `--port 8080` | Server port (default `15000`) |
| `--dir <path>` | Install directory (default `~/tofu`) |
| `--no-launch` | Install only; don't start the server |

Example:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --api-key sk-xxx --port 8080
```

---

## What the installer actually does

The installer is `install.sh`. Read the script if you want every detail;
the short version:

1. Locates conda, or installs a private Miniforge as a project sibling.
2. Creates a `tofu` conda env (Python 3.12).
3. Installs **all** Python dependencies from conda-forge — never pip.
   This avoids the `GLIBC_2.25 not found` trap that pip's manylinux
   wheels (especially `lxml`) hit on CentOS 7 / glibc 2.17.
4. Installs `ripgrep`, `fd-find`, Chromium shared libs, and PostgreSQL
   from conda-forge — no `sudo`, no system packages.
5. Installs the Playwright Chromium binary.
6. Writes `.tofu_env.json` so `python server.py` re-execs into the right
   interpreter even from an unactivated shell.
7. Starts the server.

PostgreSQL is auto-bootstrapped as a local userspace process when
available; otherwise Tofu falls back to **SQLite**, which has no
configuration and no dependencies.

---

## Troubleshooting

### "GLIBC_2.25 not found" or "GLIBC_2.28 not found" at startup

A pip-installed wheel of `lxml` (or similar) is shadowing the conda-forge
build. Fix it by re-running the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --reset-env
```

`--reset-env` deletes the existing conda env and rebuilds from scratch.
**Destructive** — only use it on the Tofu-owned env.

### Conda solver hangs forever

Almost always an outdated `conda` you installed yourself. Easiest fix:
let the installer drop a private Miniforge next to the project, and use
that instead of touching yours:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --force-sibling-conda
```

### PostgreSQL "data directory was created by major version X" error

Your `data/pgdata/` was initialized by a different PG major than the one
the installer just placed in your env. Two options:

1. **Keep your data, fall back to SQLite** — the default. The installer
   detects the mismatch and switches to SQLite automatically. Nothing
   to do.
2. **Re-initialize PG** — pass `--reinit-pgdata`. The installer renames
   the old `pgdata` to `pgdata.bak.<timestamp>` and creates a fresh one.

If you don't need PG at all, pin SQLite up front:

```bash
curl -fsSL …/install.sh | bash -s -- --force-sqlite
```

### Behind a corporate proxy that blocks GitHub releases

The Miniforge download will fail. Workarounds:

- Pre-download `Miniforge3-<platform>.sh` manually and set
  `TOFU_MINIFORGE_LOCAL=/path/to/Miniforge3-...sh` before running the
  installer.
- Or set `TOFU_MINIFORGE_MIRRORS="https://your-mirror/..."` (one URL per
  line) — the installer tries each in order before giving up.

### Install log

Every `install.sh` run writes a full transcript (ANSI colours stripped)
to `<install-dir>/logs/install-YYYYMMDD_HHMMSS.log`. Attach it when
filing an issue.

---

## Database overrides

| Env var | Effect |
|---|---|
| `TOFU_DB_BACKEND=sqlite` | Force SQLite (legacy `CHATUI_DB_BACKEND=sqlite` still works) |
| `TOFU_DB_PATH` | SQLite file location (default `data/tofu.db`) |
| `TOFU_PG_HOST` / `TOFU_PG_PORT` / `TOFU_PG_DBNAME` / `TOFU_PG_USER` / `TOFU_PG_PASSWORD` | PG DSN overrides |

---

## Docker

```bash
git clone https://github.com/rangehow/ToFu.git && cd ToFu
docker compose up -d
```

All data persists in named Docker volumes. No flags needed.
