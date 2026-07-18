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
| `--with-postgres` | Install + bootstrap PostgreSQL (opt-in). Default is SQLite — see below |
| `--use-conda` | Force the legacy conda path, skipping the default uv fast path (see below) |
| `--no-launch` | Install only; don't start the server |

Example:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --api-key sk-xxx --port 8080
```

---

## What the installer actually does

The installer is `install.sh`. It has two backends and picks the fast one
automatically:

### Fast path (default): uv

On a reasonably modern Linux host (glibc ≥ 2.28) or macOS, the installer
uses [uv](https://github.com/astral-sh/uv):

1. Ensures a `uv` binary (installs it from astral.sh if missing).
2. Creates a project-local virtualenv `.venv` from uv's own managed
   CPython (Python 3.12) — no system/conda interpreter needed.
3. `uv pip install -r requirements.txt` — installs the whole dependency
   stack from prebuilt manylinux wheels (typically ~1–2 min, **zero**
   from-source builds).
4. Runs an import smoke-test (`lxml`, `fitz`/PyMuPDF, `PIL`/Pillow,
   `cryptography`, …). **If any import fails — e.g. an old glibc with no
   compatible wheel — it falls back cleanly to the conda path.**
5. Detects a system `ripgrep`/`fd` (optional speedups; the app degrades
   to a pure-Python search fallback if absent — never a source build).
6. Installs the Playwright Chromium binary (best-effort).
7. Writes `.tofu_env.json` (`backend: uv`) so `python server.py` re-execs
   into the venv even from an unactivated shell.

### Fallback path: conda

The installer switches to conda automatically when: `--use-conda` or
`--with-postgres` was passed, the host glibc is < 2.28 (PyMuPDF/Pillow
ship no manylinux2014 wheel for it), or the uv import smoke-test failed.
The conda path:

1. Locates conda, or installs a private Miniforge as a project sibling.
2. Creates a `tofu` conda env (Python 3.12).
3. Installs **all** Python dependencies from conda-forge — never pip.
   This avoids the `GLIBC_2.25 not found` trap that pip's manylinux
   wheels (especially `lxml`) hit on CentOS 7 / glibc 2.17.
4. Installs `ripgrep`, `fd-find`, and Chromium shared libs from
   conda-forge — no `sudo`, no system packages.
5. Installs the Playwright Chromium binary.
6. Writes `.tofu_env.json` so `python server.py` re-execs into the right
   interpreter even from an unactivated shell.

Both paths then configure `.env` and start the server.

## Database: SQLite by default, PostgreSQL opt-in

By default the installer uses **SQLite** — zero configuration, no
dependencies, and fully supported by the same app code. It is fine for
single-user use and up to ~100 concurrent users.

Installing PostgreSQL is the slowest and most failure-prone part of the
setup (a layered icu/libxml2/PG-major conda solve, `initdb`, and a
startup smoke-test), so it is **no longer done by default**. Add
PostgreSQL only when you need its higher concurrency:

```bash
curl -fsSL https://raw.githubusercontent.com/rangehow/ToFu/main/install.sh \
  | bash -s -- --with-postgres
```

**Upgrading an existing SQLite install to PostgreSQL, or recovering a
previous PG dataset:** just re-run the installer with `--with-postgres`.
If you already have a `data/pgdata/` from an earlier install, that data
is reused (not lost) — the installer detects it and pins the matching PG
major. Until you pass `--with-postgres`, an existing `pgdata` is left in
place, unused, and the installer prints how to re-enable it.

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
