# ═══════════════════════════════════════════════════════════════
#  Tofu (豆腐) — Docker Image
# ═══════════════════════════════════════════════════════════════
#
#  Build:  docker build -t tofu .
#  Run:    docker run -d -p 15000:15000 -v tofu-data:/app/data --name tofu tofu
#
#  Or use docker-compose:  docker compose up -d
#
# ═══════════════════════════════════════════════════════════════

FROM python:3.12-slim AS base

# ── System dependencies ─────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
        # PostgreSQL (Debian bookworm ships v16 — compatible with PG 18+ protocol)
        postgresql-16 \
        postgresql-client-16 \
        # Build tools for compiled Python packages
        gcc g++ \
        # Playwright / browser automation system deps
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
        libcups2 libdrm2 libxkbcommon0 libxcomposite1 \
        libxdamage1 libxrandr2 libgbm1 libpango-1.0-0 \
        libcairo2 libasound2 libxshmfence1 \
        # Fast code search (ripgrep — 5x faster grep, fd-find — 3x faster find)
        ripgrep fd-find \
        # General utilities
        curl ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

# ── App directory ───────────────────────────────────────────
WORKDIR /app

# ── Python dependencies (cached layer) ──────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Playwright browser (optional — for advanced page fetching)
RUN pip install --no-cache-dir playwright \
    && playwright install chromium --with-deps 2>/dev/null || true

# ── Copy application code ──────────────────────────────────
COPY . .

# ── Create runtime directories ─────────────────────────────
RUN mkdir -p /app/data /app/logs /app/uploads

# ── Environment defaults ───────────────────────────────────
ENV PORT=15000 \
    BIND_HOST=0.0.0.0 \
    # PostgreSQL runs as a local subprocess managed by the app
    # Data lives in the mounted volume at /app/data
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# ── Expose port ────────────────────────────────────────────
EXPOSE 15000

# ── Health check ───────────────────────────────────────────
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:15000/ || exit 1

# ── Entrypoint ─────────────────────────────────────────────
# server.py auto-bootstraps PostgreSQL on first run
CMD ["python", "server.py"]
