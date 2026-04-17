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

from typing import TypedDict

from lib.log import get_logger

logger = get_logger(__name__)


# ── Types ─────────────────────────────────────────────────

class EnvSpec(TypedDict, total=False):
    """Specification for a required environment variable."""
    key: str            # env-var name (e.g. "GITHUB_TOKEN")
    label: str          # human-readable label shown in UI
    hint: str           # placeholder / help text
    required: bool      # if True, installation won't proceed without it
    secret: bool        # if True, UI renders as password field (default True)


class CatalogEntry(TypedDict, total=False):
    """One entry in the curated MCP server catalog."""
    id: str                     # unique ID used as MCP server name
    name: str                   # display name
    description: str            # one-line description
    icon: str                   # emoji or SVG string
    category: str               # for grouping in the UI
    command: str                # executable (e.g. "npx")
    args: list[str]             # argv after command
    transport: str              # "stdio" (default) or "sse"
    env_specs: list[EnvSpec]    # which env vars the user must supply
    url: str                    # homepage / docs link
    tags: list[str]             # searchable tags
    featured: bool              # show at the top of the catalog


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
CAT_OTHER   = 'Other'

CATEGORIES = [
    CAT_DEV, CAT_DATA, CAT_COMMS, CAT_SEARCH,
    CAT_PROD, CAT_DEVOPS, CAT_FINANCE, CAT_DESIGN, CAT_RESEARCH, CAT_OTHER,
]


# ══════════════════════════════════════════════════════════
#  Curated Catalog
# ══════════════════════════════════════════════════════════

CATALOG: list[CatalogEntry] = [

    # ── Development ────────────────────────────────────────

    {
        'id': 'github',
        'name': 'GitHub',
        'description': 'Manage repos, issues, PRs, code search, and more',
        'icon': '🐙',
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
        'id': 'gitlab',
        'name': 'GitLab',
        'description': 'GitLab project management, issues, and MRs',
        'icon': '🦊',
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
        'icon': '📦',
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
        'icon': '📐',
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
        'icon': '🐘',
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
        'icon': '📊',
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
        'icon': '🔴',
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
        'icon': '🍃',
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
        'icon': '💬',
        'category': CAT_COMMS,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-slack'],
        'env_specs': [
            {'key': 'SLACK_BOT_TOKEN', 'label': 'Bot Token', 'hint': 'xoxb-xxxxxxxxxxxx', 'required': True},
            {'key': 'SLACK_TEAM_ID', 'label': 'Team ID', 'hint': 'T01234567', 'required': False},
        ],
        'url': 'https://github.com/modelcontextprotocol/servers-archived/tree/main/src/slack',
        'tags': ['chat', 'messaging', 'team'],
        'featured': True,
    },
    {
        'id': 'gmail',
        'name': 'Gmail',
        'description': 'Send, search, and manage Gmail messages',
        'icon': '📧',
        'category': CAT_COMMS,
        'command': 'npx',
        'args': ['-y', '@anthropic/mcp-server-gmail'],
        'env_specs': [
            {'key': 'GMAIL_CREDENTIALS_PATH', 'label': 'Credentials JSON Path',
             'hint': '/path/to/credentials.json', 'required': True, 'secret': False},
        ],
        'url': 'https://github.com/anthropics/mcp-server-gmail',
        'tags': ['email', 'google'],
    },

    # ── Search & Web ───────────────────────────────────────

    {
        'id': 'brave-search',
        'name': 'Brave Search',
        'description': 'Web and local search via Brave Search API',
        'icon': '🦁',
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
        'icon': '🔍',
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
        'icon': '⚡',
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
        'icon': '🌐',
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
        'icon': '🔥',
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
        'icon': '📝',
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
        'icon': '✅',
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
        'icon': '📁',
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
        'icon': '🐳',
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
        'icon': '☸',
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
        'icon': '🐛',
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
        'icon': '☁️',
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
        'icon': '💳',
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
        'icon': '🎨',
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
        'icon': '🧠',
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
        'icon': '💭',
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
        'icon': '📂',
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
        'icon': '🎭',
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
        'icon': '🤖',
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
        'icon': '📚',
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
        'icon': '⚡',
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
        'icon': '▲',
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
        'icon': '☁️',
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
        'icon': '🟢',
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
        'icon': '📋',
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
        'icon': '🎯',
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
        'icon': '🎮',
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
        'icon': '🔮',
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
        'icon': '⚡',
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
        'icon': '📊',
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
        'description': 'Overleaf LaTeX projects: CRUD, compile, PDF, history, diff. Zero-config — run `overleaf_setup` tool after install.',
        'icon': '🍃',
        'category': CAT_RESEARCH,
        'command': 'overleaf-mcp',
        'args': [],
        # No env vars required — user runs the `overleaf_setup` tool for a
        # guided walkthrough, then `overleaf_save_credentials` to persist them.
        'env_specs': [],
        'url': 'https://github.com/TofuLab/overleaf-mcp',
        'tags': ['latex', 'overleaf', 'paper', 'academic', 'pdf', 'compile', 'research'],
        'featured': True,
    },

    # ── AI & Reasoning ─────────────────────────────────────

    {
        'id': 'mcp-compass',
        'name': 'MCP Compass',
        'description': 'Discover and recommend MCP servers from the ecosystem',
        'icon': '🧭',
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


def get_catalog() -> list[CatalogEntry]:
    """Return the full curated catalog."""
    return CATALOG


def get_catalog_entry(server_id: str) -> CatalogEntry | None:
    """Look up a single catalog entry by ID."""
    return _CATALOG_INDEX.get(server_id)


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

    transport = entry.get('transport', 'stdio')
    config: dict = {
        'transport': transport,
        'enabled': True,
        'description': entry.get('description', entry['name']),
    }

    if transport == 'sse':
        # SSE transport: needs a URL, no command
        config['url'] = ''  # will be set below from env_specs
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
