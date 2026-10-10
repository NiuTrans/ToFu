"""Run the installed MCP Git worker against an isolated local Git remote."""
from pathlib import Path
import sys


def worker_command(directory: Path, remote: str):
    bootstrap = directory / 'git_fixture_worker.py'
    bootstrap.write_text('''import sys
from tofu_overleaf_mcp import git_worker
remote = sys.argv.pop(1)
def configure(upstream):
    upstream.git_client._git_url = lambda project: remote
    upstream.git_client._project_tag = lambda project: project.project_id
    upstream.git_client._PUSH_RETRIES = 0
    upstream._auto_setup_guard = lambda name: None
git_worker.configure_upstream = configure
git_worker.main()
''')
    return [sys.executable, str(bootstrap), remote]
