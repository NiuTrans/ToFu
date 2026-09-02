"""lib/mcp/registry.py — Curated catalog of popular MCP servers.

Pre-packages the command / args / env-var requirements for well-known MCP
servers so that users only need to supply credentials (if any) and click
"Install".  The frontend renders this as an app-store-style grid.

Each entry is a plain dict matching :class:`CatalogEntry`.  Only ``id``,
``name``, ``command``, and ``args`` are required — everything else has
sensible defaults.

To add a new server, append an entry to ``CATALOG`` at the bottom of this
file.
"""

from __future__ import annotations

import importlib.util
import os
import threading
from typing import TypedDict

from lib.log import get_logger

logger = get_logger(__name__)

# Project root, resolved at import time so in-tree bundled MCP servers (e.g.
# github-batch under tools/) can be referenced without hardcoding an
# environment-specific absolute path (CLAUDE.md §3.5).
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_GITHUB_BATCH_PATH = os.path.join(_PROJECT_ROOT, 'tools', 'github-batch-mcp')


# ── Types ─────────────────────────────────────────────────

class EnvSpec(TypedDict, total=False):
    """Specification for a required environment variable."""
    key: str            # env-var name (e.g. "GITHUB_TOKEN")
    label: str          # human-readable label shown in UI
    hint: str           # placeholder / help text
    required: bool      # if True, installation won't proceed without it
    secret: bool        # if True, UI renders as password field (default True)
    type: str           # "text" (default) or "select" (renders a <select>)
    options: list       # for type=="select": [{label, value, autofill?}]
                        # where autofill maps other env keys → preset values
                        # applied when this option is chosen
    obtain_url: str     # WHERE to get this credential — rendered as a real
                        # "Get a key ↗" link, not prose. `hint` is a
                        # placeholder inside the input and is REPLACED by the
                        # "saved" notice on a reinstall, so a breadcrumb
                        # stuffed into it vanishes exactly when the user is
                        # rotating a key and needs it most. Keep the route
                        # here instead.
    obtain_steps: list  # optional ordered list of SHORT steps (strings) shown
                        # under the link. Use when the console path is
                        # non-obvious (e.g. Amap: create app → add key → pick
                        # "Web service"). Omit when the link is self-evident.


class CatalogEntry(TypedDict, total=False):
    """One entry in the curated MCP server catalog."""
    id: str                     # unique ID used as MCP server name
    name: str                   # display name
    description: str            # one-line description
    icon: str                   # emoji or SVG string
    category: str               # for grouping in the UI
    command: str                # executable (e.g. "npx")
    args: list[str]             # argv after command
    transport: str              # "stdio" (default) | "sse" | "streamable-http"
    endpoint: str               # remote transports: the MCP endpoint URL.
                                # Distinct from ``url`` below, which is the
                                # human docs/homepage link. When absent, the
                                # endpoint is expected to arrive via env_specs
                                # (e.g. Zapier's per-user ZAPIER_MCP_URL).
    headers: dict[str, str]     # remote transports: auth header TEMPLATE with
                                # ``${ENV_KEY}`` placeholders resolved at
                                # connect time from the server's env block.
                                # Never store a literal secret here.
    env_specs: list[EnvSpec]    # which env vars the user must supply
    url: str                    # homepage / docs link
    tags: list[str]             # searchable tags
    featured: bool              # show at the top of the catalog
    internal_only: bool         # YourProvider-internal server — hidden from
                                # opensource builds (see _OPENSOURCE_BUILD).
    health_probe: dict          # optional {tool, args, fail_patterns}: a cheap
                                # read-only tool call the bridge runs in the
                                # background to verify stored CREDENTIALS are
                                # still valid (transport health != credential
                                # health). The STANDARD contract (schema,
                                # defaults, validation, classifier) lives in
                                # lib/mcp/health_probe.py; custom servers can
                                # declare the same key in mcp_servers.json.


# ── Categories ────────────────────────────────────────────

CAT_DEV     = 'Development'
CAT_DATA    = 'Data & DB'
CAT_COMMS   = 'Communication'
CAT_SEARCH  = 'Search & Web'
CAT_PROD    = 'Productivity'
CAT_DEVOPS  = 'DevOps'
CAT_FINANCE = 'Finance'
CAT_DESIGN  = 'Design'
CAT_RESEARCH = 'Science & Research'
CAT_LOCAL_CN = 'Local Life & Travel (China)'
CAT_OTHER   = 'Other'
# Servers configured in mcp_servers.json that have no curated catalog entry
# are surfaced under this category by the API layer (see routes/api_v1/mcp.py).
CAT_CUSTOM  = 'Custom'

CATEGORIES = [
    CAT_DEV, CAT_DATA, CAT_COMMS, CAT_SEARCH,
    CAT_PROD, CAT_DEVOPS, CAT_FINANCE, CAT_DESIGN, CAT_RESEARCH,
    CAT_LOCAL_CN, CAT_OTHER, CAT_CUSTOM,
]


# ── Build edition ─────────────────────────────────────────
# Entries flagged ``internal_only`` reference YourProvider-internal services
# (hope/llm CLIs, 学城 docs) whose launchers are NOT shipped in an opensource
# build, so their catalog cards would be dead "Install" buttons there.
#
# export.py's opensource sanitizer already physically strips the internal
# block from this file's source. This flag is the BELT-AND-BRACES backstop:
# the sanitizer flips it to True (literal replacement, NOT dependent on entry
# ordering), so even if an internal entry ever escapes the source strip,
# ``get_catalog`` still filters it out at runtime. An env override is provided
# for tests / forced opensource runs.
_OPENSOURCE_BUILD = os.environ.get('TOFU_OPENSOURCE_BUILD', '1').strip().lower() in {
    '1', 'true', 'yes', 'on',
}


def is_opensource_build() -> bool:
    """True when running an opensource build (internal_only entries hidden)."""
    return _OPENSOURCE_BUILD


# ══════════════════════════════════════════════════════════
#  Curated Catalog
# ══════════════════════════════════════════════════════════

CATALOG: list[CatalogEntry] = [

    # ── Development ────────────────────────────────────────

    {
        'id': 'github',
        'name': 'GitHub',
        'description': 'Manage repos, issues, PRs, code search, and more',
        'icon': '<img src="static/icons/mcp/github.svg" alt="GitHub">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-github'],
        'env_specs': [{
            'key': 'GITHUB_PERSONAL_ACCESS_TOKEN',
            'label': 'Personal Access Token',
            'hint': 'ghp_xxxxxxxxxxxx',
            'required': True,
        }],
        'url': 'https://github.com/github/github-mcp-server',
        'tags': ['git', 'code', 'issues', 'pr'],
        'featured': True,
    },
    {
        'id': 'github-batch',
        'name': 'GitHub Batch Commit',
        'description': 'Batch-commit many files (incl. large files via Git LFS) in O(1) API calls.',
        'icon': '<img src="static/icons/mcp/github.svg" alt="GitHub">',
        'category': CAT_DEV,
        'command': 'uvx',
        'args': ['--from', _GITHUB_BATCH_PATH, 'github-batch-mcp'],
        'env_specs': [{
            'key': 'GITHUB_PERSONAL_ACCESS_TOKEN',
            'label': 'Personal Access Token',
            'hint': 'ghp_xxxxxxxxxxxx (repo scope, or contents:write)',
            'required': True,
        }],
        'url': 'https://github.com/github/github-mcp-server',
        'tags': ['git', 'commit', 'batch', 'lfs', 'bulk'],
    },
    {
        'id': 'gitlab',
        'name': 'GitLab',
        'description': 'GitLab project management, issues, and MRs',
        'icon': '<img src="static/icons/mcp/gitlab.svg" alt="GitLab">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-gitlab'],
        'env_specs': [
            {'key': 'GITLAB_PERSONAL_ACCESS_TOKEN', 'label': 'Access Token', 'hint': 'glpat-xxxx', 'required': True},
            {'key': 'GITLAB_API_URL', 'label': 'API URL', 'hint': 'https://gitlab.com/api/v4', 'required': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers',
        'tags': ['git', 'code', 'merge-request'],
    },
    {
        'id': 'git',
        'name': 'Git',
        'description': 'Read, search, and manipulate local Git repositories',
        'icon': '<img src="static/icons/mcp/git.svg" alt="Git">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-git'],
        'env_specs': [],
        'url': 'https://github.com/modelcontextprotocol/servers/tree/main/src/git',
        'tags': ['git', 'version-control'],
    },
    {
        'id': 'linear',
        'name': 'Linear',
        'description': 'Search, create, and update Linear issues and projects',
        'icon': '<img src="static/icons/mcp/linear.svg" alt="Linear">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', '@linear/mcp-server'],
        'env_specs': [
            {'key': 'LINEAR_API_KEY', 'label': 'API Key', 'hint': 'lin_api_xxxx', 'required': True},
        ],
        'url': 'https://linear.app/docs/mcp',
        'tags': ['project-management', 'issues', 'agile'],
    },

    # ── Data & DB ──────────────────────────────────────────

    {
        'id': 'postgres',
        'name': 'PostgreSQL',
        'description': 'Read-only database access with schema inspection',
        'icon': '<img src="static/icons/mcp/postgres.svg" alt="PostgreSQL">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-postgres'],
        'env_specs': [
            {'key': 'POSTGRES_CONNECTION_STRING', 'label': 'Connection String',
             'hint': 'postgresql://user:pass@localhost/dbname', 'required': True, 'secret': True},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/postgres',
        'tags': ['database', 'sql', 'postgres'],
    },
    {
        'id': 'sqlite',
        'name': 'SQLite',
        'description': 'Database interaction and business intelligence',
        'icon': '<img src="static/icons/mcp/sqlite.svg" alt="SQLite">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-sqlite'],
        'env_specs': [
            {'key': 'SQLITE_DB_PATH', 'label': 'Database Path',
             'hint': '/path/to/database.db', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sqlite',
        'tags': ['database', 'sql', 'sqlite'],
    },
    {
        'id': 'redis',
        'name': 'Redis',
        'description': 'Interact with Redis key-value stores',
        'icon': '<img src="static/icons/mcp/redis.svg" alt="Redis">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-redis'],
        'env_specs': [
            {'key': 'REDIS_URL', 'label': 'Redis URL',
             'hint': 'redis://localhost:6379', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/redis',
        'tags': ['database', 'cache', 'redis'],
    },
    {
        'id': 'mongodb',
        'name': 'MongoDB',
        'description': 'Interact with MongoDB databases and collections',
        'icon': '<img src="static/icons/mcp/mongodb.svg" alt="MongoDB">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', 'mongodb-mcp-server'],
        'env_specs': [
            {'key': 'MONGODB_URI', 'label': 'Connection URI',
             'hint': 'mongodb://localhost:27017/mydb', 'required': True, 'secret': True},
        ],
        'url': 'https://github.com/mongodb-js/mongodb-mcp-server',
        'tags': ['database', 'nosql', 'mongo'],
    },

    # ── Communication ──────────────────────────────────────

    {
        'id': 'slack',
        'name': 'Slack',
        'description': 'Channel management, messaging, and search',
        'icon': '<img src="static/icons/mcp/slack.svg" alt="Slack">',
        'category': CAT_COMMS,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-slack'],
        'env_specs': [
            {'key': 'SLACK_BOT_TOKEN', 'label': 'Bot Token',
             'hint': 'Paste the bot token from Slack', 'required': True, 'secret': True},
            {'key': 'SLACK_TEAM_ID', 'label': 'Team ID', 'hint': 'T01234567', 'required': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/slack',
        'tags': ['chat', 'messaging', 'team'],
        'featured': True,
    },
    {
        'id': 'email',
        'name': 'Email (IMAP/SMTP)',
        'description': 'Read, search, send, and reply to email over IMAP/SMTP — works with QQ Mail, 163, Outlook, Gmail, and any provider.',
        'icon': '<img src="static/icons/mcp/email.svg" alt="Email">',
        'category': CAT_COMMS,
        'command': 'uvx',
        'args': ['mcp-email-server@latest', 'stdio'],
        'env_specs': [
            {'key': 'MCP_EMAIL_SERVER_EMAIL_ADDRESS', 'label': 'Email Address',
             'hint': 'you@qq.com', 'required': True, 'secret': False},
            {'key': 'MCP_EMAIL_SERVER_PASSWORD', 'label': 'Password / Authorization Code',
             'hint': 'QQ/163: the SMTP authorization code (授权码), NOT your login password',
             'required': True, 'secret': True},
            {'key': 'MCP_EMAIL_SERVER_IMAP_HOST', 'label': 'Provider / IMAP Host',
             'hint': 'imap.qq.com (QQ) / imap.163.com / outlook.office365.com',
             'required': True, 'secret': False,
             'type': 'select',
             # Selecting a known provider auto-fills the IMAP host and the
             # SMTP host + ports (via `autofill`), so the user never has to
             # look these up. The last option drops back to manual entry.
             'options': [
                 {'label': 'QQ 邮箱', 'value': 'imap.qq.com',
                  'autofill': {'MCP_EMAIL_SERVER_SMTP_HOST': 'smtp.qq.com',
                               'MCP_EMAIL_SERVER_IMAP_PORT': '993',
                               'MCP_EMAIL_SERVER_SMTP_PORT': '465'}},
                 {'label': '163 邮箱', 'value': 'imap.163.com',
                  'autofill': {'MCP_EMAIL_SERVER_SMTP_HOST': 'smtp.163.com',
                               'MCP_EMAIL_SERVER_IMAP_PORT': '993',
                               'MCP_EMAIL_SERVER_SMTP_PORT': '465'}},
                 {'label': '126 邮箱', 'value': 'imap.126.com',
                  'autofill': {'MCP_EMAIL_SERVER_SMTP_HOST': 'smtp.126.com',
                               'MCP_EMAIL_SERVER_IMAP_PORT': '993',
                               'MCP_EMAIL_SERVER_SMTP_PORT': '465'}},
                 {'label': 'Gmail', 'value': 'imap.gmail.com',
                  'autofill': {'MCP_EMAIL_SERVER_SMTP_HOST': 'smtp.gmail.com',
                               'MCP_EMAIL_SERVER_IMAP_PORT': '993',
                               'MCP_EMAIL_SERVER_SMTP_PORT': '465'}},
                 {'label': 'Outlook / Microsoft 365', 'value': 'outlook.office365.com',
                  'autofill': {'MCP_EMAIL_SERVER_SMTP_HOST': 'smtp.office365.com',
                               'MCP_EMAIL_SERVER_IMAP_PORT': '993',
                               'MCP_EMAIL_SERVER_SMTP_PORT': '587'}},
                 {'label': 'iCloud Mail', 'value': 'imap.mail.me.com',
                  'autofill': {'MCP_EMAIL_SERVER_SMTP_HOST': 'smtp.mail.me.com',
                               'MCP_EMAIL_SERVER_IMAP_PORT': '993',
                               'MCP_EMAIL_SERVER_SMTP_PORT': '587'}},
                 {'label': '其他（手动填写 IMAP Host）', 'value': '__custom__'},
             ]},
            {'key': 'MCP_EMAIL_SERVER_SMTP_HOST', 'label': 'SMTP Host (omit for read-only)',
             'hint': 'smtp.qq.com (QQ) / smtp.163.com / smtp.office365.com', 'required': False, 'secret': False},
            {'key': 'MCP_EMAIL_SERVER_USER_NAME', 'label': 'Login Username (optional)',
             'hint': 'defaults to the email address', 'required': False, 'secret': False},
            {'key': 'MCP_EMAIL_SERVER_IMAP_PORT', 'label': 'IMAP Port (optional)',
             'hint': '993', 'required': False, 'secret': False},
            {'key': 'MCP_EMAIL_SERVER_SMTP_PORT', 'label': 'SMTP Port (optional)',
             'hint': '465', 'required': False, 'secret': False},
        ],
        'url': 'https://github.com/ai-zerolab/mcp-email-server',
        'tags': ['email', 'imap', 'smtp', 'qq', 'qq-mail', 'qqmail', '163', 'outlook', 'mail', 'inbox'],
    },

    # ── Search & Web ───────────────────────────────────────

    {
        'id': 'brave-search',
        'name': 'Brave Search',
        'description': 'Web and local search via Brave Search API',
        'icon': '<img src="static/icons/mcp/brave-search.svg" alt="Brave">',
        'category': CAT_SEARCH,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-brave-search'],
        'env_specs': [
            {'key': 'BRAVE_API_KEY', 'label': 'API Key', 'hint': 'BSAxxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/brave/brave-search-mcp-server',
        'tags': ['search', 'web'],
        'featured': True,
    },
    {
        'id': 'tavily',
        'name': 'Tavily Search',
        'description': 'AI-optimized web search with structured results',
        'icon': '<img src="static/icons/mcp/tavily.svg" alt="Tavily">',
        'category': CAT_SEARCH,
        'command': 'npx',
        'args': ['-y', 'tavily-mcp'],
        'env_specs': [
            {'key': 'TAVILY_API_KEY', 'label': 'API Key', 'hint': 'tvly-xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/tavily-ai/tavily-mcp',
        'tags': ['search', 'web', 'ai'],
    },
    {
        'id': 'exa',
        'name': 'Exa Search',
        'description': 'Neural search engine made for AI agents',
        'icon': '<img src="static/icons/mcp/exa.svg" alt="Exa">',
        'category': CAT_SEARCH,
        'command': 'npx',
        'args': ['-y', 'exa-mcp-server'],
        'env_specs': [
            {'key': 'EXA_API_KEY', 'label': 'API Key', 'hint': 'exa-xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/exa-labs/exa-mcp-server',
        'tags': ['search', 'neural', 'ai'],
    },
    {
        'id': 'fetch',
        'name': 'Fetch',
        'description': 'Web content fetching and conversion for LLM usage',
        'icon': '<img src="static/icons/mcp/fetch.svg" alt="Fetch">',
        'category': CAT_SEARCH,
        'command': 'uvx',
        'args': ['mcp-server-fetch'],
        'env_specs': [],
        'url': 'https://github.com/modelcontextprotocol/servers/tree/main/src/fetch',
        'tags': ['web', 'fetch', 'scrape'],
    },
    {
        'id': 'firecrawl',
        'name': 'Firecrawl',
        'description': 'Extract web data with advanced crawling',
        'icon': '<img src="static/icons/mcp/firecrawl.svg" alt="Firecrawl">',
        'category': CAT_SEARCH,
        'command': 'npx',
        'args': ['-y', 'firecrawl-mcp'],
        'env_specs': [
            {'key': 'FIRECRAWL_API_KEY', 'label': 'API Key', 'hint': 'fc-xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/firecrawl/firecrawl-mcp-server',
        'tags': ['web', 'crawl', 'scrape'],
    },

    # ── Productivity ───────────────────────────────────────

    {
        'id': 'notion',
        'name': 'Notion',
        'description': 'Interact with Notion pages, databases, and blocks',
        'icon': '<img src="static/icons/mcp/notion.svg" alt="Notion">',
        'category': CAT_PROD,
        'command': 'npx',
        'args': ['-y', '@notionhq/notion-mcp-server'],
        'env_specs': [
            {'key': 'OPENAPI_MCP_HEADERS', 'label': 'Auth Headers (JSON)',
             'hint': '{"Authorization":"Bearer ntn_xxx","Notion-Version":"2022-06-28"}',
             'required': True, 'secret': True},
        ],
        'url': 'https://github.com/makenotion/notion-mcp-server',
        'tags': ['notes', 'wiki', 'database'],
        'featured': True,
    },
    {
        'id': 'todoist',
        'name': 'Todoist',
        'description': 'Manage tasks, projects, and comments in Todoist',
        'icon': '<img src="static/icons/mcp/todoist.svg" alt="Todoist">',
        'category': CAT_PROD,
        'command': 'npx',
        'args': ['-y', '@doist/todoist-mcp'],
        'env_specs': [
            {'key': 'TODOIST_API_TOKEN', 'label': 'API Token', 'hint': 'xxxxxxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/doist/todoist-ai',
        'tags': ['tasks', 'todo', 'productivity'],
    },
    {
        'id': 'google-drive',
        'name': 'Google Drive',
        'description': 'File access and search in Google Drive',
        'icon': '<img src="static/icons/mcp/google-drive.svg" alt="Google Drive">',
        'category': CAT_PROD,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-gdrive'],
        'env_specs': [
            {'key': 'GDRIVE_CREDENTIALS_PATH', 'label': 'Credentials JSON',
             'hint': '/path/to/credentials.json', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/gdrive',
        'tags': ['files', 'google', 'storage'],
    },

    # ── DevOps ─────────────────────────────────────────────

    {
        'id': 'docker',
        'name': 'Docker',
        'description': 'Manage containers, images, volumes, and networks',
        'icon': '<img src="static/icons/mcp/docker.svg" alt="Docker">',
        'category': CAT_DEVOPS,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-docker'],
        'env_specs': [],
        'url': 'https://github.com/ckreiling/mcp-server-docker',
        'tags': ['container', 'docker', 'devops'],
    },
    {
        'id': 'kubernetes',
        'name': 'Kubernetes',
        'description': 'Manage pods, deployments, services in K8s clusters',
        'icon': '<img src="static/icons/mcp/kubernetes.svg" alt="Kubernetes">',
        'category': CAT_DEVOPS,
        'command': 'npx',
        'args': ['-y', 'mcp-server-kubernetes'],
        'env_specs': [],
        'url': 'https://github.com/Flux159/mcp-server-kubernetes',
        'tags': ['k8s', 'containers', 'orchestration'],
    },
    {
        'id': 'sentry',
        'name': 'Sentry',
        'description': 'Retrieve and analyze issues from Sentry',
        'icon': '<img src="static/icons/mcp/sentry.svg" alt="Sentry">',
        'category': CAT_DEVOPS,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-sentry'],
        'env_specs': [
            {'key': 'SENTRY_AUTH_TOKEN', 'label': 'Auth Token', 'hint': 'sntrys_xxxx', 'required': True},
            {'key': 'SENTRY_ORG', 'label': 'Organization Slug', 'hint': 'my-org', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/sentry',
        'tags': ['errors', 'monitoring', 'debug'],
    },
    {
        'id': 'cloudflare',
        'name': 'Cloudflare',
        'description': 'Deploy and manage Workers, KV, R2, D1',
        'icon': '<img src="static/icons/mcp/cloudflare.svg" alt="Cloudflare">',
        'category': CAT_DEVOPS,
        'command': 'npx',
        'args': ['-y', '@cloudflare/mcp-server-cloudflare'],
        'env_specs': [
            {'key': 'CLOUDFLARE_API_TOKEN', 'label': 'API Token', 'hint': 'xxxxxxxxxxxx', 'required': True},
            {'key': 'CLOUDFLARE_ACCOUNT_ID', 'label': 'Account ID', 'hint': 'xxxxxxxx', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/cloudflare/mcp-server-cloudflare',
        'tags': ['cloud', 'cdn', 'workers'],
    },

    # ── Finance ────────────────────────────────────────────

    {
        'id': 'stripe',
        'name': 'Stripe',
        'description': 'Interact with Stripe payments API',
        'icon': '<img src="static/icons/mcp/stripe.svg" alt="Stripe">',
        'category': CAT_FINANCE,
        'command': 'npx',
        'args': ['-y', '@stripe/agent-toolkit', 'mcp'],
        'env_specs': [
            {'key': 'STRIPE_SECRET_KEY', 'label': 'Secret Key', 'hint': 'sk_xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/stripe/agent-toolkit',
        'tags': ['payments', 'billing', 'fintech'],
    },

    # ── Design ─────────────────────────────────────────────

    {
        'id': 'figma',
        'name': 'Figma',
        'description': 'Access Figma design files and components',
        'icon': '<img src="static/icons/mcp/figma.svg" alt="Figma">',
        'category': CAT_DESIGN,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-figma'],
        'env_specs': [
            {'key': 'FIGMA_ACCESS_TOKEN', 'label': 'Access Token', 'hint': 'figd_xxxx', 'required': True},
        ],
        'url': 'https://github.com/figma/mcp-server-guide',
        'tags': ['design', 'ui', 'prototype'],
    },

    # ── Other ──────────────────────────────────────────────

    {
        'id': 'memory',
        'name': 'Memory',
        'description': 'Knowledge graph-based persistent memory system',
        'icon': '<img src="static/icons/mcp/memory.svg" alt="Memory">',
        'category': CAT_OTHER,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-memory'],
        'env_specs': [],
        'url': 'https://github.com/modelcontextprotocol/servers/tree/main/src/memory',
        'tags': ['memory', 'knowledge-graph', 'persistence'],
    },
    {
        'id': 'sequential-thinking',
        'name': 'Sequential Thinking',
        'description': 'Dynamic problem-solving through thought sequences',
        'icon': '<img src="static/icons/mcp/sequential-thinking.svg" alt="Sequential Thinking">',
        'category': CAT_OTHER,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-sequentialthinking'],
        'env_specs': [],
        'url': 'https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking',
        'tags': ['reasoning', 'thinking', 'problem-solving'],
    },
    {
        'id': 'filesystem',
        'name': 'Filesystem',
        'description': 'Secure file operations with access controls',
        'icon': '<img src="static/icons/mcp/filesystem.svg" alt="Filesystem">',
        'category': CAT_OTHER,
        'command': 'npx',
        'args': ['-y', '@modelcontextprotocol/server-filesystem'],
        'env_specs': [
            {'key': 'FILESYSTEM_ALLOWED_DIRS', 'label': 'Allowed Directories',
             'hint': '/home/user/documents (comma-separated)', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem',
        'tags': ['files', 'filesystem'],
    },
    {
        'id': 'playwright',
        'name': 'Playwright',
        'description': 'Browser automation for testing and scraping',
        'icon': '<img src="static/icons/mcp/playwright.svg" alt="Playwright">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', '@playwright/mcp@latest'],
        'env_specs': [],
        'url': 'https://github.com/microsoft/playwright-mcp',
        'tags': ['browser', 'testing', 'automation'],
    },
    {
        'id': 'puppeteer',
        'name': 'Puppeteer',
        'description': 'Browser automation and web scraping',
        'icon': '<img src="static/icons/mcp/puppeteer.svg" alt="Puppeteer">',
        'category': CAT_SEARCH,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-puppeteer'],
        'env_specs': [],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/puppeteer',
        'tags': ['browser', 'scrape', 'automation'],
    },

    # ── Knowledge & Documentation ──────────────────────────

    {
        'id': 'context7',
        'name': 'Context7',
        'description': 'Up-to-date, version-specific library documentation for AI coding',
        'icon': '<img src="static/icons/mcp/context7.svg" alt="Context7">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', '@upstash/context7-mcp@latest'],
        'env_specs': [],
        'url': 'https://github.com/upstash/context7',
        'tags': ['documentation', 'docs', 'libraries', 'coding', 'context'],
        'featured': True,
    },

    # ── Cloud & Infrastructure ─────────────────────────────

    {
        'id': 'supabase',
        'name': 'Supabase',
        'description': 'Query Postgres, manage edge functions, and inspect schemas',
        'icon': '<img src="static/icons/mcp/supabase.svg" alt="Supabase">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', '@supabase/mcp-server-supabase@latest', '--read-only'],
        'env_specs': [
            {'key': 'SUPABASE_ACCESS_TOKEN', 'label': 'Access Token',
             'hint': 'sbp_xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://supabase.com/docs/guides/getting-started/mcp',
        'tags': ['database', 'postgres', 'supabase', 'cloud'],
    },
    {
        'id': 'vercel',
        'name': 'Vercel',
        'description': 'Manage deployments, projects, and domains on Vercel',
        'icon': '<img src="static/icons/mcp/vercel.svg" alt="Vercel">',
        'category': CAT_DEVOPS,
        'command': 'npx',
        'args': ['-y', '@vercel/mcp@latest'],
        'env_specs': [
            {'key': 'VERCEL_API_TOKEN', 'label': 'API Token',
             'hint': 'xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://vercel.com/docs/mcp',
        'tags': ['deploy', 'hosting', 'vercel', 'frontend'],
    },
    {
        'id': 'aws',
        'name': 'AWS',
        'description': 'Manage AWS resources — S3, Lambda, EC2, CloudWatch, and more',
        'icon': '<img src="static/icons/mcp/aws.svg" alt="AWS">',
        'category': CAT_DEVOPS,
        'command': 'npx',
        'args': ['-y', '@aws/mcp@latest'],
        'env_specs': [
            {'key': 'AWS_ACCESS_KEY_ID', 'label': 'Access Key ID',
             'hint': 'AKIA...', 'required': True, 'secret': False},
            {'key': 'AWS_SECRET_ACCESS_KEY', 'label': 'Secret Access Key',
             'hint': 'xxxxxxxxxxxx', 'required': True},
            {'key': 'AWS_REGION', 'label': 'Region',
             'hint': 'us-east-1', 'required': False, 'secret': False},
        ],
        'url': 'https://awslabs.github.io/mcp/',
        'tags': ['cloud', 'aws', 'infrastructure', 's3', 'lambda'],
    },
    {
        'id': 'upstash',
        'name': 'Upstash',
        'description': 'Interact with Upstash Redis, Vector, and QStash cloud services',
        'icon': '<img src="static/icons/mcp/upstash.svg" alt="Upstash">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', '@upstash/mcp-server@latest'],
        'env_specs': [
            {'key': 'UPSTASH_EMAIL', 'label': 'Upstash Email',
             'hint': 'you@example.com', 'required': True, 'secret': False},
            {'key': 'UPSTASH_API_KEY', 'label': 'API Key',
             'hint': 'xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/upstash/mcp-server',
        'tags': ['redis', 'vector', 'cloud', 'serverless'],
    },

    # ── Project Management ─────────────────────────────────

    {
        'id': 'jira',
        'name': 'Jira',
        'description': 'Search, create, and update Jira issues and boards',
        'icon': '<img src="static/icons/mcp/jira.svg" alt="Jira">',
        'category': CAT_DEV,
        'command': 'npx',
        'args': ['-y', 'mcp-server-atlassian'],
        'env_specs': [
            {'key': 'ATLASSIAN_SITE_URL', 'label': 'Site URL',
             'hint': 'https://your-org.atlassian.net', 'required': True, 'secret': False},
            {'key': 'ATLASSIAN_USER_EMAIL', 'label': 'Email',
             'hint': 'you@example.com', 'required': True, 'secret': False},
            {'key': 'ATLASSIAN_API_TOKEN', 'label': 'API Token',
             'hint': 'xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/sooperset/mcp-atlassian',
        'tags': ['project-management', 'issues', 'agile', 'jira', 'confluence'],
    },
    {
        'id': 'asana',
        'name': 'Asana',
        'description': 'Manage tasks, projects, and workspaces in Asana',
        'icon': '<img src="static/icons/mcp/asana.svg" alt="Asana">',
        'category': CAT_PROD,
        'command': 'npx',
        'args': ['-y', '@asana/mcp-server-asana@latest'],
        'env_specs': [
            {'key': 'ASANA_ACCESS_TOKEN', 'label': 'Personal Access Token',
             'hint': '1/xxxxx:xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/Asana/asana-mcp-server',
        'tags': ['tasks', 'project-management', 'asana'],
    },

    # ── Communication (additional) ─────────────────────────

    {
        'id': 'discord',
        'name': 'Discord',
        'description': 'Read messages, manage channels, and interact with Discord servers',
        'icon': '<img src="static/icons/mcp/discord.svg" alt="Discord">',
        'category': CAT_COMMS,
        'command': 'npx',
        'args': ['-y', 'mcp-server-discord'],
        'env_specs': [
            {'key': 'DISCORD_BOT_TOKEN', 'label': 'Bot Token',
             'hint': 'xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://github.com/v-3/mcp-discord',
        'tags': ['chat', 'messaging', 'community'],
    },

    # ── Search (additional) ────────────────────────────────

    {
        'id': 'perplexity',
        'name': 'Perplexity',
        'description': 'AI-powered web search with cited answers',
        'icon': '<img src="static/icons/mcp/perplexity.svg" alt="Perplexity">',
        'category': CAT_SEARCH,
        'command': 'npx',
        'args': ['-y', 'mcp-server-perplexity'],
        'env_specs': [
            {'key': 'PERPLEXITY_API_KEY', 'label': 'API Key',
             'hint': 'pplx-xxxxxxxxxxxx', 'required': True},
        ],
        'url': 'https://docs.perplexity.ai',
        'tags': ['search', 'ai', 'research'],
    },

    # ── Automation & Integration ───────────────────────────

    {
        'id': 'zapier',
        'name': 'Zapier',
        'description': 'Connect to 8,000+ apps — Sheets, Jira, HubSpot, and more',
        'icon': '<img src="static/icons/mcp/zapier.svg" alt="Zapier">',
        'category': CAT_PROD,
        'command': '',
        'transport': 'sse',
        'args': [],
        'env_specs': [
            {'key': 'ZAPIER_MCP_URL', 'label': 'Zapier MCP URL',
             'hint': 'https://actions.zapier.com/mcp/YOUR_SERVER_ID/sse',
             'required': True, 'secret': False},
        ],
        'url': 'https://zapier.com/mcp',
        'tags': ['automation', 'integration', 'workflow', 'no-code'],
    },

    # ── Data & Analytics ───────────────────────────────────

    {
        'id': 'bigquery',
        'name': 'BigQuery',
        'description': 'Query and explore Google BigQuery datasets',
        'icon': '<img src="static/icons/mcp/bigquery.svg" alt="BigQuery">',
        'category': CAT_DATA,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-bigquery'],
        'env_specs': [
            {'key': 'GOOGLE_APPLICATION_CREDENTIALS', 'label': 'Service Account JSON Path',
             'hint': '/path/to/service-account.json', 'required': True, 'secret': False},
            {'key': 'BIGQUERY_PROJECT_ID', 'label': 'Project ID',
             'hint': 'my-project-id', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/anthropics/anthropic-quickstarts',
        'tags': ['database', 'analytics', 'google', 'bigquery'],
    },

    # ── Science & Research ──────────────────────────────────

    {
        'id': 'overleaf',
        'name': 'Overleaf',
        'description': 'Overleaf LaTeX projects: CRUD, compile, PDF, history, diff.',
        'icon': '<img src="static/icons/mcp/overleaf.svg" alt="Overleaf">',
        'category': CAT_RESEARCH,
        'command': 'uvx',
        'args': [
            '--exclude-newer', '2026-08-14T00:00:00Z',
            '--from', 'overleaf-mcp-plus[compile]==0.3.1', 'overleaf-mcp',
        ],
        'env_specs': [
            {
                'key': 'OVERLEAF_SESSION',
                'label': 'Session Cookie (overleaf_session2)',
                'hint': 's%3A... — from DevTools → Application → Cookies → overleaf.com → overleaf_session2',
                'required': True,
                'secret': True,
            },
            {
                'key': 'OVERLEAF_GIT_TOKEN',
                'label': 'Git Token (optional, for edit/read operations)',
                'hint': 'olp_... — from Overleaf → Account Settings → Git Integration → Create Token',
                'required': False,
                'secret': True,
            },
        ],
        'url': 'https://github.com/rangehow/overleaf-mcp',
        'tags': ['latex', 'overleaf', 'paper', 'academic', 'pdf', 'compile', 'research'],
        'featured': True,
        # list_projects only needs the session cookie and is a cheap read.
        # Overleaf returns auth failures as a SUCCESSFUL result whose text is an
        # error string (not an exception), so we pin the server-specific phrases
        # here; the generic auth phrases ("not authenticated", "session
        # expired", …) come free from DEFAULT_CRED_FAIL_PATTERNS. See the
        # standard contract in lib/mcp/health_probe.py.
        'health_probe': {
            'tool': 'list_projects',
            'fail_patterns': [
                'overleaf_session',
                'error fetching projects',
            ],
        },
    },

    # ── AI & Reasoning ─────────────────────────────────────

    # ── Local Life & Travel (China) ─────────────────────────
    #
    # What Chinese users actually need an agent to DO — routing, hotels,
    # flights, trains, tickets. Admission criterion, applied PER VENDOR:
    # **can a normal developer obtain credentials?**
    #
    # DELIBERATELY ABSENT — Ctrip (携程) and YourProvider (美团). Both fail that
    # criterion for POLICY reasons, so a card would be a dead Install button:
    #   • Ctrip Business Travel launched an AI open platform (2026-04) that does
    #     speak MCP — hotel/flight/train recommendation, visa policy, expense
    #     compliance — but it is gated to CORPORATE customers via a business
    #     onboarding process (ct.ctrip.com/contactBiz). Ctrip 问道 (wendao) is
    #     reachable by individuals yet is NOT MCP: a bespoke HTTP API driven by
    #     a Node CLI, with QPS/quota limits and no booking step.
    #   • YourProvider's open platform is MERCHANT-side (group-buy voucher
    #     redemption, delivery order management, storefront ops) and its
    #     five-step onboarding begins with submitting company details for
    #     business review. There is no consumer-side MCP surface at all.
    #
    # RESOLVED 2026-07-27 — the owner decided NOT to pursue corporate
    # onboarding for either vendor (option C on ticket ,
    # now CLOSED). Rationale on the record: the shipped set below already
    # covers routing / hotels / flights / trains / tickets / cruises /
    # packages, and every one of those is obtainable by an individual
    # developer, so Ctrip and YourProvider add brand familiarity plus YourProvider's
    # to-store & delivery scenarios — not reach. Reopen as a NEW ticket only
    # if a concrete to-store / delivery requirement appears, or if either
    # vendor starts issuing individual credentials (re-check per §the gate
    # below, not by assumption). Do NOT add speculative entries meanwhile:
    # test_no_dead_card_for_a_business_gated_vendor enforces this across BOTH
    # catalogues.
    #
    # ⚠ The gate is PER-VENDOR, never market-wide. An earlier revision of this
    # comment reasoned that Chinese OTAs would not open up because inventory is
    # their moat — measurement refuted it: Tuniu (2026-03) and Fliggy both ship
    # self-service, individually-obtainable credentials WITH a booking chain.
    # Fliggy is a SKILL package so it lives in lib/skills/catalog.py; the split
    # follows the PROTOCOL, not the vendor. Re-check each vendor on its own
    # evidence rather than generalising from "OTAs don't open up".

    {
        'id': 'amap-maps',
        'name': '高德地图 Amap',
        'description': 'Routing, POI/nearby search, geocoding, weather, ride-hailing and distance for mainland China — the official Amap MCP server.',
        'icon': '🗺️',
        'category': CAT_LOCAL_CN,
        'command': '',
        'transport': 'streamable-http',
        'args': [],
        # Amap authenticates by QUERY PARAM, not by header. The endpoint is a
        # template so the key still lives only in env (see lib/mcp/transport).
        'endpoint': 'https://mcp.amap.com/mcp?key=${AMAP_MAPS_API_KEY}',
        'env_specs': [
            {'key': 'AMAP_MAPS_API_KEY', 'label': 'Amap API Key (Web 服务)',
             'hint': '粘贴你的 Key',
             'obtain_url': 'https://console.amap.com/dev/key/app',
             'obtain_steps': ['登录高德开放平台并完成个人实名认证',
                              '应用管理 → 创建新应用',
                              '添加 Key，服务平台选「Web 服务」'],
             'required': True, 'secret': True},
        ],
        'url': 'https://lbs.amap.com/api/mcp-server/summary',
        'tags': ['maps', 'china', 'travel', 'routing', 'weather', 'poi',
                 '地图', '高德', '出行'],
        'featured': True,
        'install_note': '需高德开放平台实名认证个人开发者账号即可申请 Key。',
    },
    {
        'id': 'rollinggo-hotel',
        'name': 'RollingGo 酒店',
        'description': 'Hotel search with real bookable inventory and live price confirmation (2M+ hotels, 110k+ direct-contract) — free for individual developers.',
        'icon': '🏨',
        'category': CAT_LOCAL_CN,
        'command': '',
        'transport': 'streamable-http',
        'args': [],
        'endpoint': 'https://mcp.rollinggo.cn/mcp',
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'},
        'env_specs': [
            {'key': 'ROLLINGGO_API_KEY', 'label': 'RollingGo API Key',
             'hint': '粘贴你的 Key',
             'obtain_url': 'https://rollinggo.store/apply',
             'obtain_steps': ['填基本信息申请，自动审核（1-3 分钟）',
                              '同一个 Key 同时用于酒店与机票两个服务'],
             'required': True, 'secret': True},
        ],
        'url': 'https://rollinggo.store/',
        'tags': ['hotel', 'travel', 'china', 'booking', '酒店', '订房', '比价'],
        'featured': True,
    },
    {
        'id': 'rollinggo-flight',
        'name': 'RollingGo 机票',
        'description': 'Airport lookup and flight search across 500+ airlines and 200+ countries. Shares one API key with the hotel server.',
        'icon': '✈️',
        'category': CAT_LOCAL_CN,
        'command': '',
        'transport': 'streamable-http',
        'args': [],
        'endpoint': 'https://mcp.rollinggo.cn/mcp/flight',
        'headers': {'Authorization': 'Bearer ${ROLLINGGO_API_KEY}'},
        'env_specs': [
            {'key': 'ROLLINGGO_API_KEY', 'label': 'RollingGo API Key',
             'hint': '粘贴你的 Key',
             'obtain_url': 'https://rollinggo.store/apply',
             'obtain_steps': ['与酒店服务共用同一个 Key，已申请过就不用再申请'],
             'required': True, 'secret': True},
        ],
        'url': 'https://rollinggo.store/',
        'tags': ['flight', 'travel', 'china', 'booking', '机票', '航班'],
    },
    {
        'id': 'tuniu-travel',
        'name': '途牛旅游',
        'description': 'Hotels, flights, trains, attraction tickets, cruises and package tours with a FULL booking chain (search → detail → order → payment link). Six service domains behind one CLI.',
        'icon': '🐮',
        'category': CAT_LOCAL_CN,
        'command': 'npx',
        'args': ['-y', 'tuniu-cli@latest'],
        'env_specs': [
            {'key': 'TUNIU_API_KEY', 'label': '途牛开放平台 API Key',
             'hint': '粘贴你的 Key',
             'obtain_url': 'https://open.tuniu.com/mcp/login',
             'obtain_steps': ['注册并登录途牛开放平台',
                              '控制台 → API Keys → 创建应用并自助申请'],
             'required': True, 'secret': True},
        ],
        'url': 'https://open.tuniu.com/mcp/docs/',
        'tags': ['travel', 'china', 'hotel', 'flight', 'train', 'ticket',
                 'cruise', '途牛', '酒店', '机票', '门票', '邮轮', '度假'],
        'install_note': '个人开发者可自助注册申请 Key。品类最全(含邮轮/度假)且支持下单，下单后返回 paymentUrl 供用户完成支付。',
    },
    {
        'id': '12306-train',
        'name': '12306 火车票查询',
        'description': 'China Railway ticket availability, transfers and station lookup. Open-source, runs locally, query-only (no booking).',
        'icon': '🚆',
        'category': CAT_LOCAL_CN,
        'command': 'npx',
        'args': ['-y', '12306-mcp'],
        'env_specs': [],
        'url': 'https://github.com/Joooook/12306-mcp',
        'tags': ['train', 'travel', 'china', '12306', '火车票', '余票'],
        'install_note': '查询能力，不支持下单；无需 API Key。',
    },

    {
        'id': 'mcp-compass',
        'name': 'MCP Compass',
        'description': 'Discover and recommend MCP servers from the ecosystem',
        'icon': '<img src="static/icons/mcp/mcp-compass.svg" alt="MCP Compass">',
        'category': CAT_OTHER,
        'command': 'npx',
        'args': ['-y', 'mcp-compass'],
        'env_specs': [],
        'url': 'https://github.com/liuyoshio/mcp-compass',
        'tags': ['discovery', 'mcp', 'meta'],
    },
]


# ── Lookup helpers ────────────────────────────────────────

_CATALOG_INDEX: dict[str, CatalogEntry] = {e['id']: e for e in CATALOG}

# ── Hot-reload of the catalog ─────────────────────────────
#
# Mirrors the launcher-registry hot-reload in ``lib/mcp/client.py``: this
# module is imported once at startup, so a NEW card appended to ``CATALOG``
# while Tofu is already running is invisible until a restart. To keep adding
# a server truly zero-touch (the launcher row in ``vendored.py`` AND the card
# here both pick up live), we re-read THIS file when its mtime advances and
# rebuild ``CATALOG`` / ``_CATALOG_INDEX`` IN PLACE.
#
# Why in-place (mutate the existing list + dict) rather than rebinding or
# ``importlib.reload(self)``: the module's *functions* (get_catalog, …) are
# imported lazily per-request by the routes, so they always resolve to the
# live versions — but reloading the module from within its own function is
# fragile (re-runs all top-level code; swaps globals mid-call). Instead we
# exec the fresh source into a THROWAWAY namespace and copy just the data
# containers, preserving this module's identity, lock, and build-mode flag.
_catalog_mtime: float = 0.0
_catalog_reload_lock = threading.Lock()


def _registry_path() -> str:
    return os.path.abspath(__file__)


def _reload_catalog_if_changed() -> None:
    """Rebuild ``CATALOG``/``_CATALOG_INDEX`` in place if this file changed.

    Cheap: one ``os.path.getmtime`` stat per call; the actual re-exec only
    runs on a real mtime bump. Failures (file gone, syntax error mid-edit)
    are swallowed — keep the last-good catalog rather than break the settings
    grid for every request. Never DROPS the catalog to empty on a bad parse.
    """
    global _catalog_mtime
    path = _registry_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError as e:
        logger.debug('[MCP:Registry] catalog stat failed (%s) — keeping last-good', e)
        return
    if mtime <= _catalog_mtime:
        return
    with _catalog_reload_lock:
        try:
            mtime = os.path.getmtime(path)
        except OSError as e:
            logger.debug('[MCP:Registry] catalog re-stat under lock failed (%s)', e)
            return
        if mtime <= _catalog_mtime:
            return
        try:
            spec = importlib.util.spec_from_file_location(
                f'{__name__}__hotreload', path)
            fresh_mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(fresh_mod)
            fresh_catalog = list(getattr(fresh_mod, 'CATALOG', []) or [])
            if not fresh_catalog:
                raise ValueError('reloaded CATALOG is empty')
        except Exception as e:
            # Mid-edit syntax error / transient read failure — keep last-good
            # catalog and retry on the next mtime bump. Advance the baseline so
            # we don't re-exec on every request while the file is briefly broken.
            logger.warning('[MCP:Registry] catalog reload failed (keeping '
                           'last-good): %s', e)
            _catalog_mtime = mtime
            return
        before = {e['id'] for e in CATALOG}
        # Rebuild the data containers in place to preserve their identity for
        # any holder of a reference (and this module's own functions).
        CATALOG[:] = fresh_catalog
        _CATALOG_INDEX.clear()
        _CATALOG_INDEX.update({e['id']: e for e in CATALOG})
        _catalog_mtime = mtime
        added = sorted({e['id'] for e in CATALOG} - before)
        if added:
            logger.info('[MCP:Registry] catalog hot-reloaded: +%d new card(s) %s',
                        len(added), ', '.join(added))


# Baseline the mtime at import so an edit made BETWEEN process start and the
# first catalog read is still detected (mtime then strictly advances).
try:
    _catalog_mtime = os.path.getmtime(_registry_path())
except OSError as e:
    logger.debug('[MCP:Registry] catalog baseline stat failed (%s)', e)
    _catalog_mtime = 0.0


def get_catalog() -> list[CatalogEntry]:
    """Return the curated catalog.

    Hot-reloads this file first when its mtime advanced, so a card appended to
    ``CATALOG`` in the running process's source appears WITHOUT a restart.

    In an opensource build (:func:`is_opensource_build`), entries flagged
    ``internal_only`` are filtered out so the UI never shows an "Install"
    button for a launcher that isn't shipped. In internal/personal builds the
    full catalog is returned.
    """
    _reload_catalog_if_changed()
    if _OPENSOURCE_BUILD:
        return [e for e in CATALOG if not e.get('internal_only')]
    return CATALOG


def get_catalog_entry(server_id: str) -> CatalogEntry | None:
    """Look up a single catalog entry by ID.

    Hot-reloads this file first (see :func:`get_catalog`) so a freshly-added
    card is installable without a restart.

    Honours the opensource build filter: an ``internal_only`` entry is treated
    as absent in opensource builds, so install/lookup paths can't resurrect a
    hidden server by id.
    """
    _reload_catalog_if_changed()
    entry = _CATALOG_INDEX.get(server_id)
    if entry is not None and _OPENSOURCE_BUILD and entry.get('internal_only'):
        return None
    return entry


def build_server_config(server_id: str, env_values: dict[str, str] | None = None) -> dict | None:
    """Build an MCPServerConfig from a catalog entry + user-provided env values.

    Args:
        server_id: The catalog entry ID.
        env_values: Dict of env-var key → value provided by the user.

    Returns:
        A ready-to-use server config dict, or None if the server_id is unknown.
    """
    entry = get_catalog_entry(server_id)
    if entry is None:
        logger.warning('[MCP:Registry] Unknown server_id: %s', server_id)
        return None

    from lib.mcp.transport import is_stdio, normalize_transport

    config: dict = {
        'transport': normalize_transport(entry),
        'enabled': True,
        'description': entry.get('description', entry['name']),
    }

    # Per-server call-timeout override (seconds). Set for servers whose tools
    # legitimately run longer than the global MCP_CALL_TIMEOUT (e.g. long-poll
    # tools like hope.watch_job, whose own budget is 300s). Without this the
    # client transport kills the call at the 120s default and the result is
    # wasted. Users can still edit it in mcp_servers.json.
    if entry.get('timeout'):
        config['timeout'] = entry['timeout']

    if not is_stdio(config):
        # Remote transport: needs an endpoint URL, never a command. The
        # endpoint may be baked into the catalog entry or supplied per-user
        # through env_specs (handled in the loop below).
        config['url'] = entry.get('endpoint', '')
        if entry.get('headers'):
            config['headers'] = dict(entry['headers'])
    else:
        # stdio transport: needs command + args
        config['command'] = entry['command']
        config['args'] = list(entry.get('args', []))

    # Special handling: some servers take args from env vars
    # e.g. filesystem server takes allowed dirs as CLI args, not env
    env_specs = entry.get('env_specs', [])
    env: dict[str, str] = {}
    extra_args: list[str] = []

    for spec in env_specs:
        key = spec['key']
        val = (env_values or {}).get(key, '')
        if not val and spec.get('required', False):
            logger.warning('[MCP:Registry] Missing required env var %s for server %s', key, server_id)

        if key == 'FILESYSTEM_ALLOWED_DIRS':
            # Filesystem server takes directories as positional CLI args
            if val:
                extra_args.extend(d.strip() for d in val.split(',') if d.strip())
        elif key == 'POSTGRES_CONNECTION_STRING':
            # Postgres server takes connection string as CLI arg
            if val:
                extra_args.append(val)
        elif key == 'SQLITE_DB_PATH':
            # SQLite server takes db path as CLI arg
            if val:
                extra_args.append(val)
        elif key == 'REDIS_URL':
            # Redis server takes URL as CLI arg
            if val:
                extra_args.append(val)
        elif key == 'MONGODB_URI':
            # MongoDB takes URI as CLI arg
            if val:
                extra_args.append(val)
        elif key == 'ZAPIER_MCP_URL':
            # Zapier: the env var IS the SSE URL
            if val:
                config['url'] = val
        elif key == 'SUPABASE_ACCESS_TOKEN':
            # Supabase: token is passed as CLI arg --access-token
            if val:
                extra_args.extend(['--access-token', val])
        else:
            # Standard: pass as environment variable
            if val:
                env[key] = val

    if extra_args:
        config.setdefault('args', []).extend(extra_args)
    if env:
        config['env'] = env

    return config
