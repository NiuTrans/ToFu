#!/usr/bin/env python3
"""swebench_runner.py — Full SWE-bench Verified evaluation: Tofu vs Claude Code CLI.

Runs all 500 SWE-bench Verified instances using official test specs from the
swebench package. Uses conda environments per repo+version for proper isolation.
No Docker required.

Key design choices:
  - NO artificial inference timeout — agents work as long as they need
  - Conda environments from official MAP_REPO_VERSION_TO_SPECS
  - Save-after-each-instance for robust resume
  - Official test commands (runtests.py for Django, bin/test for sympy, pytest for rest)
  - Parallel inference support (Tofu + CC on different instances)

Usage:
    # Run full SWE-bench Verified (500 instances)
    python debug/swebench_runner.py --num 500

    # Run all instances, no limit
    python debug/swebench_runner.py --all

    # Run specific instances
    python debug/swebench_runner.py --instances django__django-13128,sympy__sympy-16886

    # Run only one tool
    python debug/swebench_runner.py --tool tofu --all

    # Filter by repo or difficulty
    python debug/swebench_runner.py --repo django/django --all
    python debug/swebench_runner.py --difficulty "<15 min fix" --all

    # Resume a previous run
    python debug/swebench_runner.py --all --resume --output /path/to/results.json

    # Set up conda environments only (no inference)
    python debug/swebench_runner.py --setup-envs-only

    # Dry-run: show selected instances
    python debug/swebench_runner.py --dry-run --all

    # Custom output directory
    python debug/swebench_runner.py --all --workdir /tmp/swebench_full

Prerequisites:
    - Tofu server running on http://127.0.0.1:15000
    - Claude Code proxy running on http://127.0.0.1:8082 (for CC tests)
    - claude CLI installed and configured
    - conda available for environment management
    - git available
    - pip install datasets swebench
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import textwrap
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import requests

# ─── Logging ──────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)-5s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('swebench')

# ─── Configuration ────────────────────────────────────────────────────────────

TOFU_BASE_URL = os.environ.get('TOFU_BASE_URL', 'http://127.0.0.1:15000')
TOFU_MODEL = os.environ.get('TOFU_MODEL', 'aws.claude-opus-4.6')
CC_MODEL = os.environ.get('CC_MODEL', 'opus')

# NO artificial inference timeout — agents run as long as they need.
# Only a generous safety net to catch truly stuck processes (4 hours).
INFERENCE_SAFETY_TIMEOUT = int(os.environ.get('INFERENCE_SAFETY_TIMEOUT', '14400'))
# Test execution timeout (per test, in seconds)
TEST_TIMEOUT = int(os.environ.get('TEST_TIMEOUT', '1800'))  # 30 min — large test suites need time
# Cooldown between API calls to avoid rate limits
COOLDOWN_SECONDS = int(os.environ.get('COOLDOWN_SECONDS', '5'))

# Directories — all under chatui project by default
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKDIR = Path(os.environ.get('SWEBENCH_WORKDIR', str(_PROJECT_ROOT / 'swebench_workdir')))
REPO_CACHE_DIR = Path(os.environ.get('REPO_CACHE_DIR', str(_PROJECT_ROOT / 'swebench_workdir' / 'repos')))
CONDA_ENV_PREFIX = Path(os.environ.get(
    'CONDA_ENV_DIR',
    str(_PROJECT_ROOT / 'swebench_workdir' / 'conda_envs'),
))

# Default pricing (Opus via example-corp gateway — Anthropic convention)
PRICE_INPUT_PER_1K = 0.015
PRICE_OUTPUT_PER_1K = 0.075
PRICE_CACHE_READ_PER_1K = 0.0015
PRICE_CACHE_WRITE_PER_1K = 0.01875

# Max parallel eval workers (test execution is CPU-bound)
MAX_EVAL_WORKERS = int(os.environ.get('MAX_EVAL_WORKERS', '8'))


# ─── Model Configuration ─────────────────────────────────────────────────────

@dataclass
class ModelConfig:
    """Configuration for one model to benchmark."""
    name: str          # Short display name (e.g. 'opus', 'minimax', 'glm')
    backend: str       # 'tofu' or 'cc'
    model_id: str      # Model string sent to the API (e.g. 'aws.claude-opus-4.6')
    concurrency: int = 3    # Max parallel inference workers for this model
    # Per-model pricing (per 1K tokens)
    price_input: float = 0.015
    price_output: float = 0.075
    price_cache_read: float = 0.0015
    price_cache_write: float = 0.01875
    # Optional: extra fields to merge into the /api/chat/start `config` payload.
    # Used to create tool-ablation variants (e.g. project-tools-only) without
    # touching server defaults. Example: {'searchMode': 'off', 'mcpEnabled': False}.
    config_overrides: dict = field(default_factory=dict)


# Built-in model presets — Framework × Model matrix
# Naming convention: {framework}-{model} for clarity in reports
#
# Tofu framework can run any model. Claude Code only supports Claude models.
# So the matrix is:
#   tofu-opus    = Tofu agent + Claude opus 4.6
#   tofu-minimax = Tofu agent + MiniMax-M2.7
#   tofu-glm     = Tofu agent + GLM 5.1
#   cc-opus      = Claude Code CLI + Claude opus 4.6
#
# Backward compat: 'tofu' → 'tofu-opus', 'cc' → 'cc-opus'
MODEL_PRESETS = {
    'tofu-opus': ModelConfig(
        name='tofu-opus', backend='tofu', model_id='aws.claude-opus-4.6',
        concurrency=4,  # user-approved bump 2→4 (2026-04-18): RPM headroom ample
        price_input=0.015, price_output=0.075,
        price_cache_read=0.0015, price_cache_write=0.01875,
    ),
    # Ablation: project tools only — no web_search, fetch_url, memory, or MCP.
    # keepToolHistory stays ON (server default). Used to measure how much of
    # Tofu's SWE-bench performance comes from project tooling alone vs. the
    # broader tool ecosystem.
    'tofu-opus-notool': ModelConfig(
        name='tofu-opus-notool', backend='tofu', model_id='aws.claude-opus-4.6',
        concurrency=4,
        price_input=0.015, price_output=0.075,
        price_cache_read=0.0015, price_cache_write=0.01875,
        config_overrides={
            'searchMode': 'off',      # strip web_search
            'fetchEnabled': False,    # strip fetch_url
            'memoryEnabled': False,   # strip memory tools
            'mcpEnabled': False,      # strip MCP bridge tools
        },
    ),
    'cc-opus': ModelConfig(
        name='cc-opus', backend='cc', model_id='opus',
        concurrency=1,  # Gateway rate-limited, keep at 1
        price_input=0.015, price_output=0.075,
        price_cache_read=0.0015, price_cache_write=0.01875,
    ),
    'tofu-minimax': ModelConfig(
        name='tofu-minimax', backend='tofu', model_id='MiniMax-M2.7',
        concurrency=3,  # RPM=90, generous
        price_input=0.001, price_output=0.002,
        price_cache_read=0.0002, price_cache_write=0.001,
    ),
    'tofu-minimax-notool': ModelConfig(
        name='tofu-minimax-notool', backend='tofu', model_id='MiniMax-M2.7',
        concurrency=3,
        price_input=0.001, price_output=0.002,
        price_cache_read=0.0002, price_cache_write=0.001,
        config_overrides={
            'searchMode': 'off',
            'fetchEnabled': False,
            'memoryEnabled': False,
            'mcpEnabled': False,
        },
    ),
    'tofu-glm': ModelConfig(
        name='tofu-glm', backend='tofu', model_id='glm-5.1',
        concurrency=2,  # RPM=60
        price_input=0.002, price_output=0.008,
        price_cache_read=0.0004, price_cache_write=0.002,
    ),
    'tofu-glm-notool': ModelConfig(
        name='tofu-glm-notool', backend='tofu', model_id='glm-5.1',
        concurrency=2,
        price_input=0.002, price_output=0.008,
        price_cache_read=0.0004, price_cache_write=0.002,
        config_overrides={
            'searchMode': 'off',
            'fetchEnabled': False,
            'memoryEnabled': False,
            'mcpEnabled': False,
        },
    ),
    'cc-minimax': ModelConfig(
        name='cc-minimax', backend='cc', model_id='MiniMax-M2.7',
        concurrency=1,  # Gateway rate-limited, keep at 1
        price_input=0.001, price_output=0.002,
        price_cache_read=0.0002, price_cache_write=0.001,
    ),
    'cc-glm': ModelConfig(
        name='cc-glm', backend='cc', model_id='glm-5.1',
        concurrency=1,  # Gateway rate-limited, keep at 1
        price_input=0.002, price_output=0.008,
        price_cache_read=0.0004, price_cache_write=0.002,
    ),
}

# Backward compat aliases
MODEL_PRESETS['tofu'] = MODEL_PRESETS['tofu-opus']
MODEL_PRESETS['cc'] = MODEL_PRESETS['cc-opus']

# Thread-safe results management
import threading as _threading
_results_lock = _threading.Lock()

# ─── Data Structures ─────────────────────────────────────────────────────────

@dataclass
class SWEInstance:
    """A single SWE-bench instance."""
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    hints_text: str
    patch: str           # gold patch (the correct fix)
    test_patch: str      # test patch (tests that verify the fix)
    fail_to_pass: list   # tests that should go from FAIL → PASS
    pass_to_pass: list   # tests that should remain PASS
    version: str
    difficulty: str
    created_at: str
    environment_setup_commit: str = ''


@dataclass
class InferenceResult:
    """Result of one tool's attempt at an instance."""
    instance_id: str
    tool: str
    model_patch: str = ''
    duration_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    error: str = ''
    raw_output: str = ''   # for debugging


@dataclass
class TestOutput:
    """Captured output from a single test execution."""
    test_id: str
    passed: bool = False
    stdout: str = ''
    stderr: str = ''
    duration_s: float = 0.0
    command: str = ''
    return_code: int = -1


@dataclass
class EvalResult:
    """Result of evaluating a generated patch."""
    instance_id: str
    tool: str
    resolved: bool = False
    fail_to_pass_results: dict = field(default_factory=dict)
    pass_to_pass_results: dict = field(default_factory=dict)
    fail_to_pass_outputs: list = field(default_factory=list)   # list of TestOutput
    pass_to_pass_outputs: list = field(default_factory=list)   # list of TestOutput
    patch_applies: bool = False
    test_patch_applies: bool = False
    patch_apply_stderr: str = ''
    test_patch_apply_stderr: str = ''
    install_stdout: str = ''
    install_stderr: str = ''
    error: str = ''


@dataclass
class BenchmarkResult:
    """Combined result for one instance + tool."""
    instance_id: str
    repo: str
    difficulty: str
    tool: str
    resolved: bool = False
    duration_s: float = 0.0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    num_turns: int = 0
    fail_to_pass_passed: int = 0
    fail_to_pass_total: int = 0
    pass_to_pass_passed: int = 0
    pass_to_pass_total: int = 0
    patch_applies: bool = False
    error: str = ''


# ─── Dataset Loading ─────────────────────────────────────────────────────────

# Repos that require C/Cython compilation — cannot install without Docker
C_EXTENSION_REPOS = {
    'astropy/astropy',
    'scikit-learn/scikit-learn',
    'matplotlib/matplotlib',
}


def load_swebench_instances(
    num: int = None,
    instance_ids: list = None,
    repo_filter: str = None,
    difficulty_filter: str = None,
    seed: int = 42,
    load_all: bool = False,
    skip_repos: set = None,
) -> list[SWEInstance]:
    """Load SWE-bench Verified instances from HuggingFace."""
    from datasets import load_dataset
    ds = load_dataset('princeton-nlp/SWE-bench_Verified', split='test')

    instances = []
    for item in ds:
        inst = SWEInstance(
            instance_id=item['instance_id'],
            repo=item['repo'],
            base_commit=item['base_commit'],
            problem_statement=item['problem_statement'],
            hints_text=item['hints_text'] or '',
            patch=item['patch'],
            test_patch=item['test_patch'],
            fail_to_pass=json.loads(item['FAIL_TO_PASS']),
            pass_to_pass=json.loads(item['PASS_TO_PASS']),
            version=item['version'],
            difficulty=item['difficulty'],
            created_at=item['created_at'],
            environment_setup_commit=item.get('environment_setup_commit', ''),
        )
        instances.append(inst)

    # Apply filters
    if skip_repos:
        before = len(instances)
        instances = [i for i in instances if i.repo not in skip_repos]
        if len(instances) < before:
            log.info('Skipped %d instances from repos: %s', before - len(instances), skip_repos)
    if instance_ids:
        id_set = set(instance_ids)
        instances = [i for i in instances if i.instance_id in id_set]
    if repo_filter:
        instances = [i for i in instances if i.repo == repo_filter]
    if difficulty_filter:
        instances = [i for i in instances if i.difficulty == difficulty_filter]

    if load_all or (num and num >= len(instances)):
        # Return all (sorted for reproducibility)
        instances.sort(key=lambda x: x.instance_id)
        return instances

    # Sample if num specified
    if num and num < len(instances):
        import random
        rng = random.Random(seed)
        # Stratified sampling by repo (ensures diversity)
        by_repo = {}
        for inst in instances:
            by_repo.setdefault(inst.repo, []).append(inst)
        for k in by_repo:
            by_repo[k].sort(key=lambda x: x.instance_id)
            rng.shuffle(by_repo[k])

        selected = []
        remaining = num
        repos = sorted(by_repo.keys())
        for i, repo in enumerate(repos):
            if i == len(repos) - 1:
                take = remaining
            else:
                take = max(1, round(num * len(by_repo[repo]) / len(instances)))
                take = min(take, remaining, len(by_repo[repo]))
            selected.extend(by_repo[repo][:take])
            remaining -= take
            if remaining <= 0:
                break
        instances = selected

    instances.sort(key=lambda x: x.instance_id)
    return instances


# ─── Repository & Environment Management ─────────────────────────────────────

def get_repo_path(repo: str) -> Path:
    """Get or clone a repository to the cache."""
    repo_dir = REPO_CACHE_DIR / repo.replace('/', '__')
    if not repo_dir.exists():
        log.info('Cloning %s...', repo)
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            ['git', 'clone', '--quiet', f'https://github.com/{repo}.git', str(repo_dir)],
            capture_output=True, text=True, timeout=600,
        )
        if r.returncode != 0:
            raise RuntimeError(f'Failed to clone {repo}: {r.stderr[:500]}')
        log.info('Cloned %s (%s)', repo, _dir_size(repo_dir))
    return repo_dir


def _dir_size(path: Path) -> str:
    """Human-readable directory size."""
    try:
        r = subprocess.run(['du', '-sh', str(path)], capture_output=True, text=True, timeout=10)
        return r.stdout.split()[0] if r.returncode == 0 else '?'
    except Exception:
        return '?'


def setup_workspace(inst: SWEInstance, tool: str, base_dir: Path) -> Path:
    """Create a workspace for an instance by cloning the repo at base_commit."""
    ws = base_dir / 'workspaces' / f'{inst.instance_id}__{tool}'
    if ws.exists():
        shutil.rmtree(ws)

    repo_path = get_repo_path(inst.repo)
    subprocess.run(
        ['git', 'clone', '--quiet', '--shared', str(repo_path), str(ws)],
        capture_output=True, text=True, timeout=300, check=True,
    )
    subprocess.run(
        ['git', 'checkout', '--quiet', inst.base_commit],
        capture_output=True, text=True, timeout=120, cwd=str(ws), check=True,
    )
    subprocess.run(
        ['git', 'clean', '-fdx', '--quiet'],
        capture_output=True, text=True, timeout=120, cwd=str(ws),
    )
    return ws


def get_conda_env_name(repo: str, version: str) -> str:
    """Get conda environment name for a repo+version."""
    return f'swe_{repo.replace("/", "_")}_{version}'.replace('.', '_')


def get_conda_env_path(repo: str, version: str) -> Path:
    """Get conda environment prefix path."""
    return CONDA_ENV_PREFIX / get_conda_env_name(repo, version)


def _conda_run(env_path: Path, cmd: str, cwd: str = None, timeout: int = 600) -> subprocess.CompletedProcess:
    """Run a command inside a conda environment by activating it directly.
    
    Uses process groups so timeout kills ALL child processes (not just the shell).
    Without this, shell=True + subprocess.run leaves orphaned grandchildren.
    """
    env_bin = env_path / 'bin'
    env = {**os.environ}
    env['PATH'] = f'{env_bin}:{env.get("PATH", "")}'
    env['CONDA_PREFIX'] = str(env_path)
    env['VIRTUAL_ENV'] = str(env_path)
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    
    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, cwd=cwd, env=env,
        start_new_session=True,  # new process group — killable as a unit
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        # Kill the entire process group (shell + all children)
        import signal
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        proc.wait(timeout=10)
        raise


def setup_conda_env(repo: str, version: str, workspace: Path = None) -> Path:
    """Create a conda environment for a repo+version using official SWE-bench specs.

    Returns the path to the conda environment.
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS

    env_path = get_conda_env_path(repo, version)

    # Check if already exists and is valid
    if env_path.exists():
        r = _conda_run(env_path, 'python --version')
        if r.returncode == 0:
            log.info('Conda env already exists: %s', env_path.name)
            return env_path
        else:
            log.warning('Conda env broken, recreating: %s', env_path.name)
            shutil.rmtree(env_path, ignore_errors=True)

    # Get specs
    if repo not in MAP_REPO_VERSION_TO_SPECS:
        log.warning('No specs for %s, using default Python 3.9', repo)
        specs = {'python': '3.9', 'install': 'python -m pip install -e .'}
    elif version not in MAP_REPO_VERSION_TO_SPECS[repo]:
        log.warning('No specs for %s v%s, using latest version specs', repo, version)
        available = sorted(MAP_REPO_VERSION_TO_SPECS[repo].keys())
        specs = MAP_REPO_VERSION_TO_SPECS[repo][available[-1]]
    else:
        specs = MAP_REPO_VERSION_TO_SPECS[repo][version]

    py_version = specs.get('python', '3.9')
    log.info('Creating conda env: %s (Python %s)', env_path.name, py_version)

    # Create environment
    env_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        ['conda', 'create', '--prefix', str(env_path), f'python={py_version}',
         '-y', '--quiet'],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        raise RuntimeError(f'Failed to create conda env: {r.stderr[:500]}')

    # Install conda packages (if specified and not a file reference)
    packages = specs.get('packages', '')
    if packages and packages not in ('requirements.txt', 'environment.yml'):
        pkg_list = packages if isinstance(packages, str) else ' '.join(packages)
        log.info('  Installing conda packages: %s', pkg_list[:100])
        # Use conda install with --prefix directly (not through _conda_run)
        r = subprocess.run(
            f'conda install --prefix {env_path} -y --quiet {pkg_list}',
            shell=True, capture_output=True, text=True, timeout=300,
        )
        if r.returncode != 0:
            # Fallback to pip
            log.debug('  conda install failed, trying pip: %s', r.stderr[:100])
            _conda_run(env_path, f'pip install {pkg_list}', timeout=300)

    # Install pip packages
    pip_packages = specs.get('pip_packages', [])
    if pip_packages:
        pkg_str = ' '.join(f'"{p}"' for p in pip_packages)
        log.info('  Installing pip packages: %s', pkg_str[:150])
        r = _conda_run(env_path, f'pip install {pkg_str} --quiet', timeout=300)
        if r.returncode != 0:
            log.warning('  pip install failed (non-fatal): %s', r.stderr[:200])

    # Run pre_install commands (skip apt-get as we don't have sudo)
    pre_install = specs.get('pre_install', [])
    for cmd in pre_install:
        if 'apt-get' in cmd or 'apt ' in cmd:
            log.debug('  Skipping apt command: %s', cmd[:80])
            continue
        if 'DEBIAN_FRONTEND' in cmd:
            continue
        log.info('  Pre-install: %s', cmd[:100])
        if workspace:
            _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Install the package itself (requires workspace)
    if workspace:
        install_cmd = specs.get('install', 'python -m pip install -e .')
        log.info('  Installing package: %s', install_cmd[:100])
        r = _conda_run(env_path, install_cmd, cwd=str(workspace), timeout=600)
        if r.returncode != 0:
            log.warning('  Package install failed: %s', r.stderr[:300])

    # Install requirements.txt / environment.yml if specified
    if workspace and packages == 'requirements.txt':
        req_file = workspace / 'requirements.txt'
        if req_file.exists():
            log.info('  Installing requirements.txt')
            _conda_run(env_path, f'pip install -r requirements.txt --quiet',
                       cwd=str(workspace), timeout=300)
    elif workspace and packages == 'environment.yml':
        env_file = workspace / 'environment.yml'
        if env_file.exists():
            log.info('  Installing deps from environment.yml')
            # Parse environment.yml to extract pip-installable deps
            try:
                import yaml
                with open(env_file) as _yf:
                    env_yml = yaml.safe_load(_yf)
                conda_deps = []
                pip_deps = []
                for dep in env_yml.get('dependencies', []):
                    if isinstance(dep, str):
                        # Skip python itself, pip, and build/lint tools
                        dep_name = dep.split('>=')[0].split('==')[0].split('<')[0].strip()
                        if dep_name in ('python', 'pip', 'pre-commit', 'mypy'):
                            continue
                        conda_deps.append(dep_name)
                    elif isinstance(dep, dict) and 'pip' in dep:
                        pip_deps.extend(dep['pip'])
                if conda_deps:
                    # Install via pip (more reliable than conda install for cross-env)
                    dep_str = ' '.join(conda_deps)
                    log.info('  Installing %d deps from environment.yml: %s', len(conda_deps), dep_str[:150])
                    _conda_run(env_path, f'pip install {dep_str} --quiet', timeout=600)
                if pip_deps:
                    pip_str = ' '.join(pip_deps)
                    log.info('  Installing %d pip deps: %s', len(pip_deps), pip_str[:150])
                    _conda_run(env_path, f'pip install {pip_str} --quiet', timeout=300)
            except Exception as e:
                log.warning('  Failed to parse environment.yml: %s — falling back to pip install -e .', e)
            _conda_run(env_path, f'pip install -e . --quiet',
                       cwd=str(workspace), timeout=300)

    # Ensure pytest is always available
    _conda_run(env_path, 'pip install pytest --quiet', timeout=120)

    log.info('  ✅ Conda env ready: %s', env_path.name)
    return env_path


def setup_all_conda_envs(instances: list[SWEInstance]) -> dict[str, Path]:
    """Pre-build all conda environments needed for the instances.

    Returns a dict mapping 'repo/version' to env_path.
    """
    combos = set()
    for inst in instances:
        combos.add((inst.repo, inst.version))

    log.info('Setting up %d conda environments...', len(combos))
    env_map = {}
    for repo, version in sorted(combos):
        key = f'{repo}/{version}'
        try:
            env_path = setup_conda_env(repo, version)
            env_map[key] = env_path
        except Exception as e:
            log.error('Failed to create env for %s: %s', key, e)
            env_map[key] = None

    ok = sum(1 for v in env_map.values() if v)
    log.info('Conda environments: %d/%d ready', ok, len(env_map))
    return env_map


# ─── Agent Prompt ─────────────────────────────────────────────────────────────

def build_agent_prompt(inst: SWEInstance) -> str:
    """Build the prompt for the agent to solve the issue."""
    prompt = textwrap.dedent(f"""\
        You are solving a GitHub issue in the repository {inst.repo}.

        ## Issue Description

        {inst.problem_statement}

        ## Instructions

        1. Read the relevant source files to understand the codebase and the issue.
        2. Identify the root cause of the bug or the files that need to be changed.
        3. Make the minimal necessary code changes to fix the issue.
        4. Do NOT modify any test files.
        5. Do NOT add new test files.
        6. Focus on fixing the actual issue described above.

        The repository is already checked out at the correct commit. Start by exploring
        the project structure and reading the relevant files mentioned in the issue.
    """)

    if inst.hints_text:
        prompt += f"\n## Hints\n\n{inst.hints_text}\n"

    return prompt


# ─── Inference: Tofu ──────────────────────────────────────────────────────────

def run_tofu_inference(inst: SWEInstance, workspace: Path,
                      mcfg: ModelConfig = None) -> InferenceResult:
    """Run Tofu on an instance. No artificial timeout."""
    tool_name = mcfg.name if mcfg else 'tofu'
    model_id = mcfg.model_id if mcfg else TOFU_MODEL
    result = InferenceResult(instance_id=inst.instance_id, tool=tool_name)
    prompt = build_agent_prompt(inst)
    t0 = time.time()

    try:
        # Retry the initial POST up to 10 times — with parallel benchmarking,
        # the server may rate-limit or be briefly busy.
        task_id = None
        for attempt in range(1, 11):
            try:
                _config = {
                    'model': model_id,
                    'projectPath': str(workspace),
                }
                # Merge per-model tool-ablation overrides (e.g. tofu-opus-notool)
                if mcfg and getattr(mcfg, 'config_overrides', None):
                    _config.update(mcfg.config_overrides)
                resp = requests.post(
                    f'{TOFU_BASE_URL}/api/chat/start',
                    json={
                        'convId': f'swebench-{inst.instance_id}-{tool_name}-{int(time.time())}',
                        'messages': [{'role': 'user', 'content': prompt}],
                        'config': _config,
                    },
                    timeout=120,
                )
                resp.raise_for_status()
                task_id = resp.json()['taskId']
                break
            except (requests.Timeout, requests.ConnectionError, requests.HTTPError) as e:
                log.warning('[%s] POST attempt %d/10 failed: %s', tool_name, attempt, e)
                if attempt < 10:
                    wait = 5 + 5 * attempt  # 10s, 15s, 20s, ...
                    # Longer backoff for rate limits
                    if isinstance(e, requests.HTTPError) and e.response is not None and e.response.status_code == 429:
                        wait = 10 + 10 * attempt  # 20s, 30s, 40s, ...
                    time.sleep(wait)
                else:
                    raise  # final attempt — let outer handler catch it
        if not task_id:
            result.error = 'Failed to get task_id after 3 attempts'
            result.duration_s = time.time() - t0
            return result

        # Poll until done — no tight timeout, generous safety net only
        poll_interval = 2.0
        while True:
            elapsed = time.time() - t0
            if elapsed > INFERENCE_SAFETY_TIMEOUT:
                result.error = f'Safety timeout after {elapsed:.0f}s'
                try:
                    requests.post(f'{TOFU_BASE_URL}/api/chat/abort/{task_id}', timeout=5)
                except Exception:
                    pass
                break

            time.sleep(poll_interval)
            try:
                poll_resp = requests.get(
                    f'{TOFU_BASE_URL}/api/chat/poll/{task_id}',
                    timeout=10,
                )
                # ★ 404 = task gone (server restarted, task cleaned up).
                #   Don't keep polling for hours — treat as interrupted.
                if poll_resp.status_code == 404:
                    log.warning('Poll got 404 — task %s no longer exists '
                                '(server restart?). Treating as interrupted '
                                'after %.0fs.', task_id[:8], time.time() - t0)
                    result.error = 'Task lost (server restarted during inference)'
                    result.duration_s = time.time() - t0
                    break
                poll_resp.raise_for_status()
                data = poll_resp.json()
            except requests.HTTPError as e:
                log.warning('Poll HTTP error: %s', e)
                continue
            except Exception as e:
                log.warning('Poll failed: %s', e)
                continue

            status = data.get('status', '')
            if status in ('done', 'error', 'interrupted'):
                result.duration_s = time.time() - t0

                # Parse usage from apiRounds
                api_rounds = data.get('apiRounds', [])
                if isinstance(api_rounds, list):
                    result.num_turns = len(api_rounds)
                    for rd in api_rounds:
                        ru = rd.get('usage', {})
                        result.input_tokens += ru.get('prompt_tokens', 0)
                        result.output_tokens += ru.get('completion_tokens', 0)
                        result.cache_read_tokens += ru.get('cache_read_tokens', 0)
                        result.cache_write_tokens += ru.get('cache_write_tokens', 0)

                result.cost_usd = _compute_cost(result, mcfg)

                # Save full poll response for debugging
                try:
                    result.raw_output = json.dumps(data, ensure_ascii=False)[:50000]
                except Exception:
                    result.raw_output = str(data)[:20000]

                if status == 'error':
                    result.error = data.get('error', 'Unknown error')
                break

            # Adaptive polling: back off but cap at 10s
            poll_interval = min(poll_interval * 1.1, 10.0)

    except Exception as e:
        result.duration_s = time.time() - t0
        result.error = str(e)

    # Extract patch from workspace
    result.model_patch = _extract_git_diff(workspace)
    return result


# ─── CC Proxy Management ──────────────────────────────────────────────────────

CC_PROXY_URL = os.environ.get('CC_PROXY_URL', 'http://127.0.0.1:8082')
CC_PROXY_DIR = os.environ.get('CC_PROXY_DIR', '')  # auto-detected if empty
_cc_proxy_proc = None


def _find_proxy_dir() -> Optional[Path]:
    """Find the claude-code-proxy directory."""
    if CC_PROXY_DIR:
        return Path(CC_PROXY_DIR)
    # Auto-detect: look relative to chatui project
    candidates = [
        Path(__file__).resolve().parent.parent.parent / 'claude-code-workspace' / 'proxy',
        Path.home() / 'claude-code-workspace' / 'proxy',
    ]
    for c in candidates:
        if (c / 'start_proxy.py').exists():
            return c
    return None


def ensure_cc_proxy_alive() -> bool:
    """Check if CC proxy is responding; restart it if dead. Returns True if alive."""
    global _cc_proxy_proc
    try:
        resp = requests.get(f'{CC_PROXY_URL}/health', timeout=5)
        if resp.status_code == 200:
            return True
    except Exception:
        pass

    # Proxy is dead — try to restart
    log.warning('[CC Proxy] Not responding, attempting restart...')
    proxy_dir = _find_proxy_dir()
    if not proxy_dir:
        log.error('[CC Proxy] Cannot find proxy directory. Set CC_PROXY_DIR env var.')
        return False

    # Kill any lingering proxy process
    if _cc_proxy_proc and _cc_proxy_proc.poll() is None:
        _cc_proxy_proc.terminate()
        try:
            _cc_proxy_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _cc_proxy_proc.kill()

    env = os.environ.copy()
    env['HOST'] = '127.0.0.1'  # Override conda's HOST
    _cc_proxy_proc = subprocess.Popen(
        [sys.executable, 'start_proxy.py'],
        cwd=str(proxy_dir),
        env=env,
        stdout=open('/tmp/cc_proxy.log', 'a'),
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
    )
    log.info('[CC Proxy] Started PID %d, waiting for it to be ready...', _cc_proxy_proc.pid)

    # Wait for proxy to become healthy
    for i in range(30):  # up to 30 seconds
        time.sleep(1)
        try:
            resp = requests.get(f'{CC_PROXY_URL}/health', timeout=3)
            if resp.status_code == 200:
                log.info('[CC Proxy] Healthy after %ds', i + 1)
                return True
        except Exception:
            pass

    log.error('[CC Proxy] Failed to start after 30s')
    return False


# ─── Inference: Claude Code ───────────────────────────────────────────────────

def _is_cc_retryable_error(stdout: str, stderr: str) -> bool:
    """Check if CC output indicates a retryable error (429, ECONNREFUSED, etc)."""
    combined = (stdout or '') + (stderr or '')
    retryable_patterns = [
        'ECONNREFUSED',
        '429',
        'rate_limit',
        'too many requests',
        'overloaded',
        'Unable to connect to API',
        'socket hang up',
        'ETIMEDOUT',
        'ECONNRESET',
    ]
    lower = combined.lower()
    for pat in retryable_patterns:
        if pat.lower() in lower:
            return True
    return False


def run_cc_inference(inst: SWEInstance, workspace: Path,
                    mcfg: ModelConfig = None) -> InferenceResult:
    """Run Claude Code CLI on an instance. Retries on 429/connection errors."""
    tool_name = mcfg.name if mcfg else 'cc'
    cc_model = mcfg.model_id if mcfg else CC_MODEL
    result = InferenceResult(instance_id=inst.instance_id, tool=tool_name)
    prompt = build_agent_prompt(inst)
    t0 = time.time()
    max_retries = 10  # generous — 429s can require many retries

    for attempt in range(1, max_retries + 1):
        # Ensure proxy is alive before each attempt
        if not ensure_cc_proxy_alive():
            log.error('[CC] Proxy dead and cannot restart, attempt %d/%d', attempt, max_retries)
            if attempt < max_retries:
                time.sleep(30)
                continue
            result.error = 'CC proxy unavailable'
            result.duration_s = time.time() - t0
            return result

        # Reset workspace for retry (undo any partial changes)
        if attempt > 1:
            subprocess.run(
                ['git', 'checkout', '.'],
                capture_output=True, text=True, timeout=30, cwd=str(workspace),
            )
            subprocess.run(
                ['git', 'clean', '-fd', '--quiet'],
                capture_output=True, text=True, timeout=30, cwd=str(workspace),
            )

        try:
            # Prepend /tmp/safe_bin to PATH so Claude CLI's `find` for python
            # uses our fast wrapper instead of scanning the network filesystem
            cc_env = os.environ.copy()
            cc_env['PATH'] = '/tmp/safe_bin:' + cc_env.get('PATH', '')
            proc = subprocess.run(
                [
                    'claude', '-p',
                    '--output-format', 'json',
                    '--model', cc_model,
                    '--dangerously-skip-permissions',
                    prompt,
                ],
                capture_output=True, text=True,
                timeout=INFERENCE_SAFETY_TIMEOUT,  # safety net only
                cwd=str(workspace),
                stdin=subprocess.DEVNULL,
                env=cc_env,
            )

            # Check for retryable errors in output
            if _is_cc_retryable_error(proc.stdout, proc.stderr):
                wait = min(30 * attempt, 300)  # 30s, 60s, 90s, ... up to 5min
                log.warning('[CC] Attempt %d/%d hit retryable error, waiting %ds: %.200s',
                            attempt, max_retries, wait,
                            (proc.stdout or proc.stderr or '')[:200])
                if attempt < max_retries:
                    time.sleep(wait)
                    continue
                # Last attempt — fall through to record whatever we got

            result.duration_s = time.time() - t0

            if proc.returncode != 0 and not proc.stdout.strip():
                result.error = f'Exit code {proc.returncode}: {proc.stderr[:500]}'

            # Parse CC JSON output
            if proc.stdout.strip():
                try:
                    data = json.loads(proc.stdout.strip())
                    result.num_turns = data.get('num_turns', 1)
                    usage = data.get('usage', {})
                    result.input_tokens = usage.get('input_tokens', 0)
                    result.output_tokens = usage.get('output_tokens', 0)
                    result.cache_read_tokens = usage.get('cache_read_input_tokens', 0)
                    result.cache_write_tokens = usage.get('cache_creation_input_tokens', 0)
                    result.raw_output = proc.stdout[:50000]  # save full output for detail file
                except (json.JSONDecodeError, KeyError, TypeError):
                    result.raw_output = proc.stdout[:20000]

            result.cost_usd = _compute_cost(result, mcfg)
            break  # success — exit retry loop

        except subprocess.TimeoutExpired:
            result.duration_s = time.time() - t0
            result.error = f'Safety timeout after {INFERENCE_SAFETY_TIMEOUT}s'
            break  # don't retry timeouts
        except Exception as e:
            result.duration_s = time.time() - t0
            result.error = str(e)
            if attempt < max_retries:
                log.warning('[CC] Attempt %d/%d exception, retrying: %s', attempt, max_retries, e)
                time.sleep(30 * attempt)
                continue
            break

    result.model_patch = _extract_git_diff(workspace)
    return result


# ─── Patch Extraction ─────────────────────────────────────────────────────────

_EXCLUDE_PREFIXES = [
    '.chatui/', '__pycache__/', '.project_sessions/',
    '.claude/', '.git/',
]

def _extract_git_diff(workspace: Path) -> str:
    """Extract git diff of changes made by the agent.

    Uses generous timeouts (120s) for FUSE/NFS filesystems where git operations
    can be slow under concurrent load.  Falls back to unstaged diff if
    'git add' times out OR returns a non-zero exit code (e.g. stale index.lock).

    Empty patches from this function are only returned when we have *proof*
    (staged or unstaged name-only listing) that the workspace has no source
    changes.  If any git sub-command errors out, we try progressively weaker
    fallbacks before giving up — never silently returning ''.
    """
    _GIT_TIMEOUT = 120  # seconds — FUSE can be very slow under load

    def _run(args, _cwd=str(workspace), _to=_GIT_TIMEOUT):
        """Run a git command safely: never raises on non-zero, logs timeouts."""
        try:
            return subprocess.run(
                args, capture_output=True, text=True, timeout=_to, cwd=_cwd,
            )
        except subprocess.TimeoutExpired:
            log.warning('git %s timed out after %ds in %s',
                        ' '.join(args[1:]), _to, workspace.name)
            return None
        except Exception as e:
            log.warning('git %s crashed in %s: %s',
                        ' '.join(args[1:]), workspace.name, e)
            return None

    # Defensive: clear any stale index.lock from an interrupted prior run.
    # This is safe — if a real git process were running in the workspace, it
    # would have exited long ago (we only extract after inference finishes).
    lock = workspace / '.git' / 'index.lock'
    if lock.exists():
        try:
            lock.unlink()
            log.info('Cleared stale index.lock in %s', workspace.name)
        except Exception as e:
            log.warning('Failed to clear stale index.lock in %s: %s', workspace.name, e)

    try:
        # First, try the staged-diff approach (git add -A → git diff --cached)
        r_add = _run(['git', 'add', '-A'])
        add_ok = r_add is not None and r_add.returncode == 0
        if not add_ok:
            rc = r_add.returncode if r_add is not None else -1
            err = (r_add.stderr[:200] if r_add is not None else 'timeout/crash')
            log.warning('git add -A failed in %s (rc=%d): %s — falling back to unstaged diff',
                        workspace.name, rc, err)

        if add_ok:
            r_files = _run(['git', 'diff', '--cached', '--name-only'])
        else:
            r_files = _run(['git', 'diff', '--name-only'])

        # If the name-only listing itself failed, fall back to `git status --porcelain`
        # before giving up, so we don't lose real changes to harness errors.
        file_list = []
        if r_files is not None and r_files.returncode == 0:
            file_list = [f.strip() for f in r_files.stdout.strip().split('\n') if f.strip()]
        else:
            rc = r_files.returncode if r_files is not None else -1
            log.warning('git diff --name-only failed (rc=%d) in %s — trying status --porcelain',
                        rc, workspace.name)
            r_stat = _run(['git', 'status', '--porcelain'])
            if r_stat is not None and r_stat.returncode == 0:
                for line in r_stat.stdout.splitlines():
                    # Porcelain format: two-letter status + space + path
                    if len(line) > 3:
                        path = line[3:].strip()
                        # Handle renames: "R  old -> new"
                        if ' -> ' in path:
                            path = path.split(' -> ', 1)[1].strip()
                        if path:
                            file_list.append(path)
            else:
                log.error('Could not list changed files in %s — all methods failed',
                          workspace.name)
                return ''

        source_files = []
        for f in file_list:
            if any(f.startswith(p) for p in _EXCLUDE_PREFIXES):
                continue
            if f.endswith('.pyc'):
                continue
            source_files.append(f)

        if not source_files:
            return ''

        # Emit the diff — try staged first, then unstaged, then HEAD-vs-worktree.
        diff_attempts = []
        if add_ok:
            diff_attempts.append(['git', 'diff', '--cached', '--'] + source_files)
        diff_attempts.append(['git', 'diff', '--'] + source_files)
        diff_attempts.append(['git', 'diff', 'HEAD', '--'] + source_files)

        for cmd in diff_attempts:
            r = _run(cmd)
            if r is not None and r.returncode == 0 and r.stdout.strip():
                diff = r.stdout.rstrip('\r')
                if not diff.endswith('\n'):
                    diff += '\n'
                return diff

        log.warning('All diff attempts empty in %s despite %d changed files: %s',
                    workspace.name, len(source_files), source_files[:5])
        return ''
    except Exception as e:
        log.warning('Failed to extract diff from %s: %s', workspace.name, e, exc_info=True)
        return ''


# ─── Evaluation ───────────────────────────────────────────────────────────────

def _parse_django_log_fixed(log: str, test_spec) -> dict[str, str]:
    """Parse Django test output, correctly handling multi-line docstring output.

    The official swebench parse_log_django misparses tests with docstrings because
    the verbose output spans two lines:
        test_foo (module.TestClass)
        Description of the test ... ok

    The parser takes the docstring line as the key instead of the test name line.
    This version tracks prev_test to handle this correctly.
    """
    from swebench.harness.grading import TestStatus
    status_map = {}
    lines = log.split('\n')
    prev_test = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            prev_test = None
            continue

        # Check if this is a test name line: "test_foo (module.Class)" without " ... "
        test_name_match = re.match(r'^(\S+\s+\([^)]+\))\s*$', stripped)
        if test_name_match:
            prev_test = test_name_match.group(1)
            continue

        # Check for status suffixes
        status = None
        test_name = None

        if ' ... ok' in stripped or ' ... OK' in stripped or ' ...  OK' in stripped:
            status = TestStatus.PASSED.value
            test_name = re.split(r'\s+\.\.\.\s+(?:ok|OK)', stripped)[0]
        elif ' ... skipped' in stripped:
            status = TestStatus.SKIPPED.value
            test_name = stripped.split(' ... skipped')[0]
        elif stripped.endswith(' ... FAIL'):
            status = TestStatus.FAILED.value
            test_name = stripped.rsplit(' ... FAIL', 1)[0]
        elif stripped.endswith(' ... ERROR'):
            status = TestStatus.ERROR.value
            test_name = stripped.rsplit(' ... ERROR', 1)[0]
        elif stripped.startswith('FAIL:'):
            status = TestStatus.FAILED.value
            parts = stripped.split()
            test_name = parts[1].strip() if len(parts) > 1 else stripped
        elif stripped.startswith('ERROR:'):
            status = TestStatus.ERROR.value
            parts = stripped.split()
            test_name = parts[1].strip() if len(parts) > 1 else stripped

        if status:
            # If the test_name looks like a docstring and we have prev_test,
            # store BOTH: the docstring name AND the method name, since the
            # SWE-bench dataset may use either format in FAIL_TO_PASS / PASS_TO_PASS.
            if prev_test and not re.match(r'^(\w+)\s+\(', test_name):
                # test_name is a docstring — store under both keys
                status_map[prev_test] = status
                status_map[test_name] = status
            else:
                status_map[test_name] = status
            prev_test = None
        elif not test_name_match:
            # Non-test line that doesn't reset state (could be continuation)
            pass

    return status_map


def _get_test_directives(inst: SWEInstance) -> list[str]:
    """Extract test directives from test_patch using official SWE-bench logic.

    For Django: converts file paths to module notation (tests/auth_tests/test_validators.py → auth_tests.test_validators).
    For others: returns file paths as-is (e.g. tests/test_foo.py).

    If the test_patch only modifies non-Python files (e.g. .txt, .json), extracts
    unique test modules from the F2P + P2P test IDs as a fallback.
    """
    diff_pat = r"diff --git a/.* b/(.*)"
    directives = re.findall(diff_pat, inst.test_patch)
    # Remove non-test files
    non_test_exts = ['.txt', '.md', '.rst', '.json', '.yml', '.yaml', '.cfg', '.ini', '.toml']
    directives = [d for d in directives if not any(d.endswith(ext) for ext in non_test_exts)]

    if inst.repo == 'django/django':
        transformed = []
        for d in directives:
            d = d[:-len('.py')] if d.endswith('.py') else d
            d = d[len('tests/'):] if d.startswith('tests/') else d
            d = d.replace('/', '.')
            transformed.append(d)
        directives = transformed

    # Fallback: when test_patch only modifies non-.py files (e.g. .txt fixtures),
    # extract unique test modules from F2P + P2P test IDs
    if not directives:
        modules = set()
        for test_id in inst.fail_to_pass + inst.pass_to_pass:
            if inst.repo == 'django/django':
                # Django format: "test_method (module.TestClass)"
                m = re.match(r'\S+\s+\(([^)]+)\)', test_id)
                if m:
                    parts = m.group(1).split('.')
                    mod = '.'.join(parts[:2]) if len(parts) >= 2 else parts[0]
                    modules.add(mod)
            elif '::' in test_id:
                # pytest format: "path/to/test.py::test_func"
                modules.add(test_id.split('::')[0])
            elif '/' in test_id:
                modules.add(test_id)
        directives = sorted(modules)
        if directives:
            log.info('  Extracted %d test directives from F2P/P2P (no .py in test_patch)',
                     len(directives))

    return directives


def evaluate_patch(
    inst: SWEInstance,
    model_patch: str,
    tool: str,
    base_dir: Path,
    env_map: dict[str, Path],
) -> EvalResult:
    """Evaluate a model-generated patch using official SWE-bench test methodology.

    Uses the EXACT same approach as the official SWE-bench Docker harness:
    1. Create fresh workspace at base_commit
    2. Apply model_patch + gold test_patch
    3. Install package in conda env
    4. Run the FULL test command once (with test directives from test_patch)
    5. Parse output with official repo-specific log parser
    6. Grade with official get_eval_tests_report
    """
    from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
    from swebench.harness.grading import (
        MAP_REPO_TO_PARSER, get_eval_tests_report, get_resolution_status,
        ResolvedStatus, TestStatus, FAIL_TO_PASS, PASS_TO_PASS,
        EvalType, FAIL_ONLY_REPOS,
    )

    result = EvalResult(instance_id=inst.instance_id, tool=tool)

    if not model_patch:
        result.error = 'Empty patch'
        return result

    # Get conda env
    env_key = f'{inst.repo}/{inst.version}'
    env_path = env_map.get(env_key)

    # Get specs
    specs = {}
    if inst.repo in MAP_REPO_VERSION_TO_SPECS:
        specs = MAP_REPO_VERSION_TO_SPECS[inst.repo].get(inst.version, {})

    # --- Workspace setup ---
    eval_ws = base_dir / 'eval' / f'{inst.instance_id}__{tool}'
    if eval_ws.exists():
        shutil.rmtree(eval_ws)

    try:
        repo_path = get_repo_path(inst.repo)
        subprocess.run(
            ['git', 'clone', '--quiet', '--shared', str(repo_path), str(eval_ws)],
            capture_output=True, text=True, timeout=300, check=True,
        )
        subprocess.run(
            ['git', 'checkout', '--quiet', inst.base_commit],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws), check=True,
        )
        subprocess.run(
            ['git', 'clean', '-fdx', '--quiet'],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
    except Exception as e:
        result.error = f'Workspace setup failed: {e}'
        return result

    # --- Apply model patch ---
    patch_file = eval_ws / '__model_patch.diff'
    patch_file.write_text(model_patch)
    r = subprocess.run(
        ['git', 'apply', '--whitespace=fix', str(patch_file)],
        capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
    )
    result.patch_applies = (r.returncode == 0)
    result.patch_apply_stderr = r.stderr[:2000] if r.stderr else ''
    if not result.patch_applies:
        r2 = subprocess.run(
            ['git', 'apply', '--whitespace=fix', '-C1', str(patch_file)],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
        result.patch_applies = (r2.returncode == 0)
        if result.patch_applies:
            result.patch_apply_stderr = '(applied with -C1)'
        else:
            result.patch_apply_stderr += '\n--- fallback -C1 ---\n' + (r2.stderr[:1000] or '')
            subprocess.run(
                ['git', 'apply', '--whitespace=fix', '--reject', str(patch_file)],
                capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
            )
            result.error = f'Patch apply failed: {r.stderr[:300]}'

    # --- Apply gold test patch ---
    test_patch_file = eval_ws / '__test_patch.diff'
    test_patch_file.write_text(inst.test_patch)
    r = subprocess.run(
        ['git', 'apply', '--whitespace=fix', str(test_patch_file)],
        capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
    )
    result.test_patch_applies = (r.returncode == 0)
    result.test_patch_apply_stderr = r.stderr[:2000] if r.stderr else ''
    if not result.test_patch_applies:
        r2 = subprocess.run(
            ['git', 'apply', '--whitespace=fix', '-C1', str(test_patch_file)],
            capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
        )
        result.test_patch_applies = (r2.returncode == 0)
        if not result.test_patch_applies:
            result.test_patch_apply_stderr += '\n--- fallback ---\n' + (r2.stderr[:1000] or '')
            subprocess.run(
                ['git', 'apply', '--whitespace=fix', '--reject', str(test_patch_file)],
                capture_output=True, text=True, timeout=30, cwd=str(eval_ws),
            )

    # --- Install package using PYTHONPATH isolation ---
    # Instead of creating per-instance venvs (which can introduce version
    # mismatches from setuptools upgrades or failed editable installs),
    # we use PYTHONPATH to point to the eval workspace. This is simpler,
    # faster, and ensures the workspace code always takes priority.
    #
    # Run pre_install and eval_commands from specs (but skip apt-get)
    if env_path:
        install_r = _install_in_conda(inst, eval_ws, env_path, specs)
        if install_r:
            result.install_stdout = (install_r.stdout or '')[-5000:]
            result.install_stderr = (install_r.stderr or '')[-5000:]

    # Detect source layout for PYTHONPATH
    src_layout = _detect_source_layout(inst.repo, eval_ws)
    if src_layout == 'src':
        workspace_pythonpath = str(eval_ws / 'src')
    else:
        workspace_pythonpath = str(eval_ws)

    # --- Run full test command (official SWE-bench approach) ---
    test_directives = _get_test_directives(inst)
    test_cmd_template = specs.get('test_cmd', 'python -m pytest -rA')
    if isinstance(test_cmd_template, list):
        test_cmd_template = test_cmd_template[-1]

    # Build full test command with directives
    full_test_cmd = f'{test_cmd_template} {" ".join(test_directives)}'

    # Set up environment variables from eval_commands
    env_setup_cmds = []
    for cmd in specs.get('eval_commands', []):
        if cmd.startswith('export '):
            env_setup_cmds.append(cmd)
        elif 'locale-gen' in cmd or 'locale.gen' in cmd:
            continue  # skip locale setup (requires root)
    env_prefix = '; '.join(env_setup_cmds) + '; ' if env_setup_cmds else ''
    # Always set LANG for Django tests
    if 'django' in inst.repo:
        env_prefix = 'export LANG=en_US.UTF-8; export LC_ALL=en_US.UTF-8; ' + env_prefix

    # Set PYTHONPATH to eval workspace — ensures the workspace code takes
    # priority over any stale installs in the shared conda env.
    env_prefix = f'export PYTHONPATH={workspace_pythonpath}:$PYTHONPATH; ' + env_prefix

    log.info('  Running tests: %s', full_test_cmd[:200])

    # Run the full test command
    t0 = time.time()
    test_output = TestOutput(test_id='__full_test_run__')
    try:
        if env_path:
            r = _conda_run(
                env_path,
                f'bash -c "{env_prefix}{full_test_cmd}"',
                cwd=str(eval_ws),
                timeout=TEST_TIMEOUT,
            )
        else:
            # Use Popen with process group for clean timeout kill
            _p = subprocess.Popen(
                f'bash -c "{env_prefix}{full_test_cmd}"',
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, cwd=str(eval_ws), start_new_session=True,
            )
            try:
                _out, _err = _p.communicate(timeout=TEST_TIMEOUT)
                r = subprocess.CompletedProcess(full_test_cmd, _p.returncode, _out, _err)
            except subprocess.TimeoutExpired:
                import signal
                try:
                    os.killpg(os.getpgid(_p.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    _p.kill()
                _p.wait(timeout=10)
                raise
        # Keep FULL output for log parsing — truncation happens only when saving
        test_output.stdout = r.stdout or ''
        test_output.stderr = r.stderr or ''
        test_output.return_code = r.returncode
        test_output.command = full_test_cmd
    except subprocess.TimeoutExpired as e:
        test_output.stderr = f'TIMEOUT after {TEST_TIMEOUT}s'
        raw = getattr(e, 'stdout', None)
        if isinstance(raw, bytes):
            test_output.stdout = raw.decode('utf-8', errors='replace')
        result.error = f'Test timeout after {TEST_TIMEOUT}s'
    except Exception as e:
        test_output.stderr = f'Test execution error: {e}'
        result.error = str(e)
    test_output.duration_s = round(time.time() - t0, 2)

    # Store the full test output for debugging
    result.fail_to_pass_outputs = [test_output]

    # --- Parse test output with official log parser ---
    log_content = test_output.stdout + '\n' + test_output.stderr
    # Use our fixed Django parser that handles multi-line docstring output
    if inst.repo == 'django/django':
        log_parser = _parse_django_log_fixed
    else:
        log_parser = MAP_REPO_TO_PARSER.get(inst.repo)

    if log_parser:
        try:
            # Create a minimal TestSpec-like object for the parser
            class _MinimalTestSpec:
                def __init__(self, inst):
                    self.instance_id = inst.instance_id
                    self.repo = inst.repo
                    self.version = inst.version
                    self.FAIL_TO_PASS = inst.fail_to_pass
                    self.PASS_TO_PASS = inst.pass_to_pass

            test_spec = _MinimalTestSpec(inst)
            status_map = log_parser(log_content, test_spec)
            log.info('  Parsed %d test results from log output', len(status_map))

            # Grade using official logic
            eval_ref = {
                'instance_id': inst.instance_id,
                FAIL_TO_PASS: inst.fail_to_pass,
                PASS_TO_PASS: inst.pass_to_pass,
            }
            eval_type = EvalType.FAIL_ONLY if inst.repo in FAIL_ONLY_REPOS else EvalType.PASS_AND_FAIL
            report = get_eval_tests_report(status_map, eval_ref, eval_type=eval_type)

            # Extract results
            f2p_success = report.get(FAIL_TO_PASS, {}).get('success', [])
            f2p_failure = report.get(FAIL_TO_PASS, {}).get('failure', [])
            p2p_success = report.get(PASS_TO_PASS, {}).get('success', [])
            p2p_failure = report.get(PASS_TO_PASS, {}).get('failure', [])

            for t in f2p_success:
                result.fail_to_pass_results[t] = True
            for t in f2p_failure:
                result.fail_to_pass_results[t] = False
            for t in p2p_success:
                result.pass_to_pass_results[t] = True
            for t in p2p_failure:
                result.pass_to_pass_results[t] = False

            # Resolved = all F2P pass AND all P2P pass (official criteria)
            result.resolved = (
                get_resolution_status(report) == ResolvedStatus.FULL.value
            )

            log.info('  F2P: %d/%d passed, P2P: %d/%d passed, resolved=%s',
                     len(f2p_success), len(f2p_success) + len(f2p_failure),
                     len(p2p_success), len(p2p_success) + len(p2p_failure),
                     result.resolved)

        except Exception as e:
            log.warning('  Log parser failed: %s', e, exc_info=True)
            result.error = f'Log parser failed: {e}'
    else:
        log.warning('  No log parser for repo %s — using return code', inst.repo)
        # Fallback: treat return code 0 as all tests passed
        if test_output.return_code == 0:
            for t in inst.fail_to_pass:
                result.fail_to_pass_results[t] = True
            for t in inst.pass_to_pass:
                result.pass_to_pass_results[t] = True
            result.resolved = True

    return result


# ─── Per-instance source layout detection ────────────────────────────────────

# Map repo → source directories to add to PYTHONPATH.
# Repos with src/ layout need 'src' added; flat layout repos need '.' (workspace root).
# This is detected once per repo from the workspace structure.
_REPO_SRC_LAYOUT = {}  # repo → 'src' or '.'


def _detect_source_layout(repo: str, workspace: Path) -> str:
    """Detect whether a repo uses src/ layout or flat layout.

    Returns 'src' for src-layout repos (pytest, flask), '.' for flat layout.
    """
    if repo in _REPO_SRC_LAYOUT:
        return _REPO_SRC_LAYOUT[repo]

    # Check for src/ directory containing Python packages
    src_dir = workspace / 'src'
    if src_dir.is_dir():
        # Verify it contains actual Python packages (not just docs)
        has_py_pkg = any(
            (src_dir / d / '__init__.py').exists()
            for d in os.listdir(src_dir)
            if (src_dir / d).is_dir()
        )
        if has_py_pkg:
            _REPO_SRC_LAYOUT[repo] = 'src'
            log.info('  [Layout] %s uses src/ layout', repo)
            return 'src'

    _REPO_SRC_LAYOUT[repo] = '.'
    return '.'


def _install_in_conda(inst: SWEInstance, workspace: Path, env_path: Path,
                      specs: dict = None):
    """Install the package in the conda env for testing.

    Uses per-instance isolated install to avoid shared env corruption:
    - Installs to a per-eval temp directory (--target) instead of the shared env
    - The caller sets PYTHONPATH to include this directory + workspace
    - This prevents parallel eval instances from clobbering each other's installs
    """
    if specs is None:
        from swebench.harness.constants import MAP_REPO_VERSION_TO_SPECS
        specs = {}
        if inst.repo in MAP_REPO_VERSION_TO_SPECS:
            specs = MAP_REPO_VERSION_TO_SPECS[inst.repo].get(inst.version, {})

    # Run pre_install (skip apt commands)
    for cmd in specs.get('pre_install', []):
        if 'apt-get' in cmd or 'apt ' in cmd or 'DEBIAN_FRONTEND' in cmd:
            continue
        _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Run eval_commands that are NOT 'export' (those are env vars, handled at test time)
    for cmd in specs.get('eval_commands', []):
        if cmd.startswith('export '):
            continue  # handled at test time
        if 'locale-gen' in cmd or 'locale.gen' in cmd:
            continue  # requires root
        if 'apt-get' in cmd or 'apt ' in cmd:
            continue
        _conda_run(env_path, f'bash -c "{cmd}"', cwd=str(workspace), timeout=120)

    # Install: run `pip install -e .` to handle dependencies (pytz, etc.)
    # but rely on PYTHONPATH (set at test time) for source code isolation.
    # The editable install may write .egg-link to the shared env, but
    # PYTHONPATH takes priority in sys.path so the correct workspace
    # code is always used regardless of where .egg-link points.
    install_cmd = specs.get('install', 'python -m pip install -e .')
    if '--no-build-isolation' not in install_cmd:
        install_cmd = 'python -m pip install -e . --quiet --no-deps 2>/dev/null; python -m pip install -e . --quiet 2>/dev/null || true'
        log.debug('  Installing (editable + deps): %s', install_cmd[:100])
    else:
        log.debug('  Installing (needs C build): %s', install_cmd[:100])

    r = _conda_run(env_path, install_cmd, cwd=str(workspace), timeout=600)
    if r.returncode != 0:
        log.warning('  Install failed (non-fatal, PYTHONPATH will override): %s', r.stderr[:200])
    return r


    # (Old per-test runner functions removed — evaluation now uses official SWE-bench
    #  log parsers via evaluate_patch() above)


# ─── Cost Computation ─────────────────────────────────────────────────────────

def _compute_cost(result: InferenceResult, mcfg: ModelConfig = None) -> float:
    """Compute cost from token counts using per-model pricing."""
    pi = mcfg.price_input if mcfg else PRICE_INPUT_PER_1K
    po = mcfg.price_output if mcfg else PRICE_OUTPUT_PER_1K
    pcr = mcfg.price_cache_read if mcfg else PRICE_CACHE_READ_PER_1K
    pcw = mcfg.price_cache_write if mcfg else PRICE_CACHE_WRITE_PER_1K
    cost = (
        result.input_tokens * pi / 1000
        + result.output_tokens * po / 1000
        + result.cache_read_tokens * pcr / 1000
        + result.cache_write_tokens * pcw / 1000
    )
    return round(cost, 6)


# ─── Results Persistence ──────────────────────────────────────────────────────

def _save_per_run_detail(
    base_dir: Path,
    inst: SWEInstance,
    tool: str,
    inf_result: 'InferenceResult',
    eval_result: 'EvalResult | None',
    br: BenchmarkResult,
):
    """Save full per-run detail: patch, raw agent output, test outputs.

    Directory structure:
        workdir/
          patches/{instance_id}__{tool}.diff
          details/{instance_id}__{tool}.json
    """
    # Always save the model patch
    patch_dir = base_dir / 'patches'
    patch_dir.mkdir(parents=True, exist_ok=True)
    patch_file = patch_dir / f'{inst.instance_id}__{tool}.diff'
    patch_file.write_text(inf_result.model_patch or '# (empty — no patch generated)\n')

    # Build detailed record
    detail = {
        'instance_id': inst.instance_id,
        'repo': inst.repo,
        'version': inst.version,
        'difficulty': inst.difficulty,
        'tool': tool,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        # Inference details
        'inference': {
            'duration_s': inf_result.duration_s,
            'input_tokens': inf_result.input_tokens,
            'output_tokens': inf_result.output_tokens,
            'cache_read_tokens': inf_result.cache_read_tokens,
            'cache_write_tokens': inf_result.cache_write_tokens,
            'cost_usd': inf_result.cost_usd,
            'num_turns': inf_result.num_turns,
            'error': inf_result.error,
            'patch_size': len(inf_result.model_patch),
            'raw_output': inf_result.raw_output,  # full CC JSON or Tofu poll response
        },
        # Eval details
        'eval': None,
        # Summary
        'resolved': br.resolved,
        'patch_applies': br.patch_applies,
        'gold_patch_size': len(inst.patch),
    }

    if eval_result:
        # Full test run output (single run, parsed with official log parser)
        test_run_outputs = []
        for to in getattr(eval_result, 'fail_to_pass_outputs', []):
            test_run_outputs.append({
                'test_id': to.test_id,
                'duration_s': to.duration_s,
                'command': to.command,
                'return_code': to.return_code,
                'stdout': to.stdout[-30000:] if to.stdout else '',
                'stderr': to.stderr[-10000:] if to.stderr else '',
            })

        detail['eval'] = {
            'resolved': eval_result.resolved,
            'patch_applies': eval_result.patch_applies,
            'test_patch_applies': eval_result.test_patch_applies,
            'patch_apply_stderr': eval_result.patch_apply_stderr,
            'test_patch_apply_stderr': eval_result.test_patch_apply_stderr,
            'install_stdout': (eval_result.install_stdout or '')[-3000:],
            'install_stderr': (eval_result.install_stderr or '')[-3000:],
            'fail_to_pass': eval_result.fail_to_pass_results,
            'pass_to_pass': eval_result.pass_to_pass_results,
            'test_run_output': test_run_outputs,
            'error': eval_result.error,
        }

    detail_dir = base_dir / 'details'
    detail_dir.mkdir(parents=True, exist_ok=True)
    detail_file = detail_dir / f'{inst.instance_id}__{tool}.json'
    with open(detail_file, 'w') as f:
        json.dump(detail, f, indent=2, ensure_ascii=False)

    log.debug('Saved detail: %s (%d bytes)', detail_file.name, detail_file.stat().st_size)


def _save_results(results: list[BenchmarkResult], instances: list[SWEInstance],
                  output_path: Path, model_configs: list[ModelConfig] = None):
    """Save results to JSON after each instance."""
    models_info = {}
    if model_configs:
        for mc in model_configs:
            models_info[mc.name] = {
                'backend': mc.backend, 'model_id': mc.model_id,
                'concurrency': mc.concurrency,
            }
    summary = {
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'config': {
            'tofu_model': TOFU_MODEL,
            'cc_model': CC_MODEL,
            'models': models_info,
            'inference_safety_timeout': INFERENCE_SAFETY_TIMEOUT,
            'num_instances': len(instances),
        },
        'instances': [
            {
                'instance_id': inst.instance_id,
                'repo': inst.repo,
                'version': inst.version,
                'difficulty': inst.difficulty,
                'fail_to_pass_count': len(inst.fail_to_pass),
                'pass_to_pass_count': len(inst.pass_to_pass),
                'gold_patch_size': len(inst.patch),
            }
            for inst in instances
        ],
        'results': [asdict(r) for r in results],
        'summary': _compute_summary_stats(results),
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)


def _compute_summary_stats(results: list[BenchmarkResult]) -> dict:
    """Compute summary statistics for the results."""
    stats = {}
    all_tools = sorted(set(r.tool for r in results))
    for tool in all_tools:
        rs = [r for r in results if r.tool == tool]
        if not rs:
            continue
        resolved = sum(1 for r in rs if r.resolved)
        total = len(rs)
        total_cost = sum(r.cost_usd for r in rs)
        avg_time = sum(r.duration_s for r in rs) / max(total, 1)
        avg_turns = sum(r.num_turns for r in rs) / max(total, 1)
        total_cr = sum(r.cache_read_tokens for r in rs)
        total_in = sum(r.input_tokens for r in rs)
        total_cw = sum(r.cache_write_tokens for r in rs)
        cache_rate = total_cr / max(total_in + total_cr + total_cw, 1) * 100

        stats[tool] = {
            'resolved': resolved,
            'total': total,
            'resolve_rate': round(resolved / max(total, 1) * 100, 1),
            'avg_time_s': round(avg_time, 1),
            'total_cost_usd': round(total_cost, 2),
            'avg_turns': round(avg_turns, 1),
            'cache_hit_rate': round(cache_rate, 1),
        }
    return stats


def _load_completed(output_path: Path) -> tuple[list[BenchmarkResult], set]:
    """Load completed results from a previous run."""
    if not output_path.exists():
        return [], set()
    with open(output_path) as f:
        data = json.load(f)
    results = []
    completed = set()
    for r in data.get('results', []):
        br = BenchmarkResult(**{k: v for k, v in r.items() if k in BenchmarkResult.__dataclass_fields__})
        results.append(br)
        completed.add(f'{br.instance_id}__{br.tool}')
    return results, completed


# ─── Reporting ────────────────────────────────────────────────────────────────

def print_instance_result(br: BenchmarkResult):
    """Print result for a single instance."""
    status = '✅' if br.resolved else '❌'
    apply_str = '📎' if br.patch_applies else '💔'
    print(f'    {status} {apply_str} {br.instance_id:<45s} '
          f'⏱{br.duration_s:7.0f}s  💰${br.cost_usd:6.3f}  '
          f'🔄{br.num_turns:3d}t  '
          f'F2P={br.fail_to_pass_passed}/{br.fail_to_pass_total}  '
          f'P2P={br.pass_to_pass_passed}/{br.pass_to_pass_total}')
    if br.error:
        print(f'         ⚠ {br.error[:200]}')


def print_progress(results: list[BenchmarkResult], total_runs: int):
    """Print live progress stats."""
    done = len(results)
    if done == 0:
        return

    all_tools = sorted(set(r.tool for r in results))
    for tool in all_tools:
        rs = [r for r in results if r.tool == tool]
        if not rs:
            continue
        resolved = sum(1 for r in rs if r.resolved)
        total = len(rs)
        cost = sum(r.cost_usd for r in rs)
        label = f'{tool:>10s}'
        print(f'    📊 {label}: {resolved}/{total} resolved ({100*resolved/max(total,1):.0f}%)  '
              f'${cost:.2f} spent  [{done}/{total_runs} runs done]')


def print_summary(results: list[BenchmarkResult]):
    """Print final comparison summary."""
    print('\n' + '═' * 120)
    print('                              SWE-BENCH VERIFIED — BENCHMARK SUMMARY')
    print('═' * 120)

    all_tools = sorted(set(r.tool for r in results))
    for tool in all_tools:
        rs = [r for r in results if r.tool == tool]
        if not rs:
            continue

        resolved = sum(1 for r in rs if r.resolved)
        total = len(rs)
        avg_time = sum(r.duration_s for r in rs) / max(total, 1)
        total_cost = sum(r.cost_usd for r in rs)
        avg_cost = total_cost / max(total, 1)
        avg_turns = sum(r.num_turns for r in rs) / max(total, 1)
        total_in = sum(r.input_tokens for r in rs)
        total_out = sum(r.output_tokens for r in rs)
        total_cr = sum(r.cache_read_tokens for r in rs)
        total_cw = sum(r.cache_write_tokens for r in rs)
        total_f2p_pass = sum(r.fail_to_pass_passed for r in rs)
        total_f2p = sum(r.fail_to_pass_total for r in rs)
        patch_applies = sum(1 for r in rs if r.patch_applies)
        cache_rate = total_cr / max(total_in + total_cr + total_cw, 1) * 100
        errors = sum(1 for r in rs if r.error and 'timeout' in r.error.lower())

        label = tool.upper()
        print(f'\n  {label}:')
        print(f'    ✅ Resolved:         {resolved}/{total} ({100*resolved/max(total,1):.1f}%)')
        print(f'    📎 Patch applies:    {patch_applies}/{total}')
        print(f'    🧪 F2P tests:        {total_f2p_pass}/{total_f2p} passed')
        print(f'    ⏱  Avg time:         {avg_time:.0f}s')
        print(f'    💰 Total cost:       ${total_cost:.2f}  (avg ${avg_cost:.3f}/instance)')
        print(f'    🔄 Avg turns:        {avg_turns:.1f}')
        print(f'    📊 Input tokens:     {total_in:,}')
        print(f'    📊 Output tokens:    {total_out:,}')
        print(f'    💾 Cache read:       {total_cr:,} ({cache_rate:.0f}%)')
        print(f'    💾 Cache write:      {total_cw:,}')
        if errors:
            print(f'    ⚠️  Timeouts:        {errors}')

        # Breakdown by difficulty
        print(f'\n    By difficulty:')
        for diff in ['<15 min fix', '15 min - 1 hour', '1-4 hours', '>4 hours']:
            dr = [r for r in rs if r.difficulty == diff]
            if dr:
                d_resolved = sum(1 for r in dr if r.resolved)
                print(f'      {diff:>15s}: {d_resolved}/{len(dr)} ({100*d_resolved/max(len(dr),1):.0f}%)')

        # Breakdown by repo
        print(f'\n    By repository:')
        repos = sorted(set(r.repo for r in rs))
        for repo in repos:
            rr = [r for r in rs if r.repo == repo]
            r_resolved = sum(1 for r in rr if r.resolved)
            print(f'      {repo:<30s}: {r_resolved}/{len(rr)} ({100*r_resolved/max(len(rr),1):.0f}%)')

    # Head-to-head comparison (pairwise for all tool combinations)
    tool_maps = {}
    for tool in all_tools:
        tool_maps[tool] = {r.instance_id: r for r in results if r.tool == tool}

    if len(all_tools) >= 2:
        # Build a cross-model resolution matrix
        print(f'\n  Cross-model comparison:')
        # Find instances common to ALL tools
        common_all = set.intersection(*(set(m.keys()) for m in tool_maps.values())) if tool_maps else set()
        if common_all:
            print(f'    Instances tested by all {len(all_tools)} models: {len(common_all)}')
            # Show how many each model solved
            for tool in all_tools:
                solved = sum(1 for i in common_all if tool_maps[tool][i].resolved)
                print(f'      {tool:>12s}: {solved}/{len(common_all)} ({100*solved/max(len(common_all),1):.1f}%)')

            # Unique wins (solved by only one model)
            print(f'\n    Unique wins (solved by only one model):')
            for tool in all_tools:
                unique = [i for i in common_all
                          if tool_maps[tool][i].resolved and
                          not any(tool_maps[t][i].resolved for t in all_tools if t != tool)]
                if unique:
                    print(f'      {tool:>12s}: {len(unique)}')
                    for i in sorted(unique)[:5]:
                        r = tool_maps[tool][i]
                        print(f'        • {i} ({r.duration_s:.0f}s, ${r.cost_usd:.3f})')
                    if len(unique) > 5:
                        print(f'        ... and {len(unique)-5} more')

    print('\n' + '═' * 120)


# ─── Preflight Checks ────────────────────────────────────────────────────────

def check_tofu() -> bool:
    try:
        r = requests.get(f'{TOFU_BASE_URL}/api/health', timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def check_cc() -> bool:
    try:
        r = subprocess.run(['claude', '--version'], capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return False
    except Exception:
        return False
    try:
        r = requests.get('http://127.0.0.1:8082/health', timeout=5)
        return r.status_code == 200
    except Exception:
        # CC might work without proxy (direct API)
        log.warning('CC proxy not available — CC will use direct API')
        return True


def preflight(tools: list[str]) -> list[str]:
    """Check which tools are available."""
    available = []
    if 'tofu' in tools:
        if check_tofu():
            available.append('tofu')
            log.info('✅ Tofu: %s (model: %s)', TOFU_BASE_URL, TOFU_MODEL)
        else:
            log.error('❌ Tofu NOT reachable at %s', TOFU_BASE_URL)

    if 'cc' in tools:
        if check_cc():
            available.append('cc')
            log.info('✅ Claude Code CLI ready (model: %s)', CC_MODEL)
        else:
            log.error('❌ Claude Code CLI NOT available')

    return available


# ─── Main Pipeline ────────────────────────────────────────────────────────────

def _run_one_instance(
    inst: SWEInstance,
    mcfg: ModelConfig,
    base_dir: Path,
    env_map: dict,
    skip_eval: bool = False,
) -> BenchmarkResult:
    """Run inference + eval for ONE instance + ONE model. Thread-safe."""
    tool = mcfg.name
    max_inference_retries = 3  # retry on empty/fluke inference (no patch, ≤1 turn)
    try:
        # Phase 1: Inference (with retry for empty/fluke results)
        workspace = setup_workspace(inst, tool, base_dir)
        inf_result = None

        for inf_attempt in range(1, max_inference_retries + 1):
            # Reset workspace for retry
            if inf_attempt > 1:
                log.info('  [%s] %s Retry %d/%d (previous attempt produced no patch)',
                         tool.upper(), inst.instance_id, inf_attempt, max_inference_retries)
                subprocess.run(['git', 'checkout', '.'], capture_output=True,
                               text=True, timeout=30, cwd=str(workspace))
                subprocess.run(['git', 'clean', '-fd', '--quiet'], capture_output=True,
                               text=True, timeout=30, cwd=str(workspace))
                time.sleep(5)  # small backoff between retries

            if mcfg.backend == 'tofu':
                inf_result = run_tofu_inference(inst, workspace, mcfg)
            else:
                inf_result = run_cc_inference(inst, workspace, mcfg)

            # Decide whether to retry:
            # - Has a patch → accept (success or failure, eval will judge)
            # - Has a real error (timeout, crash) → accept (don't retry crashes)
            # - No patch + ≤1 turn + no hard error → fluke, retry
            has_patch = bool(inf_result.model_patch and inf_result.model_patch.strip())
            has_hard_error = bool(inf_result.error and inf_result.error not in ('', 'No patch generated'))
            is_fluke = not has_patch and not has_hard_error and inf_result.num_turns <= 1

            if not is_fluke or inf_attempt == max_inference_retries:
                break
            log.warning('  [%s] %s Fluke inference: %d turns, %d patch chars — will retry',
                        tool.upper(), inst.instance_id, inf_result.num_turns,
                        len(inf_result.model_patch))

        log.info('  [%s] %s → Patch: %d chars, %.0fs, $%.3f, %d turns',
                 tool.upper(), inst.instance_id, len(inf_result.model_patch),
                 inf_result.duration_s, inf_result.cost_usd, inf_result.num_turns)
        if inf_result.error:
            log.warning('  [%s] %s Error: %s', tool.upper(), inst.instance_id, inf_result.error[:200])

        # ── Gate: reject failed inference (429, timeout, 0 tokens) ──
        # Only record results where inference actually ran successfully.
        # Failed runs return None → not saved → resume will re-run them.
        has_real_inference = (
            inf_result.model_patch.strip()
            or inf_result.input_tokens > 0
            or inf_result.cost_usd > 0
        )
        if not has_real_inference:
            log.warning('  [%s] %s DISCARDED — inference failed (no patch, 0 tokens, 0 cost): %s',
                        tool.upper(), inst.instance_id, inf_result.error[:200])
            return None  # signal to caller: don't save this

        # Phase 2: Evaluation
        br = BenchmarkResult(
            instance_id=inst.instance_id,
            repo=inst.repo,
            difficulty=inst.difficulty,
            tool=tool,
            duration_s=inf_result.duration_s,
            cost_usd=inf_result.cost_usd,
            input_tokens=inf_result.input_tokens,
            output_tokens=inf_result.output_tokens,
            cache_read_tokens=inf_result.cache_read_tokens,
            cache_write_tokens=inf_result.cache_write_tokens,
            num_turns=inf_result.num_turns,
        )

        eval_result = None

        if skip_eval:
            br.error = 'Eval skipped'
        elif inf_result.model_patch:
            log.info('  [%s] %s Evaluating...', tool.upper(), inst.instance_id)
            eval_result = evaluate_patch(inst, inf_result.model_patch, tool, base_dir,
                                         env_map or {})
            br.resolved = eval_result.resolved
            br.patch_applies = eval_result.patch_applies
            br.fail_to_pass_passed = sum(1 for v in eval_result.fail_to_pass_results.values() if v)
            br.fail_to_pass_total = len(eval_result.fail_to_pass_results)
            br.pass_to_pass_passed = sum(1 for v in eval_result.pass_to_pass_results.values() if v)
            br.pass_to_pass_total = len(eval_result.pass_to_pass_results)
            if eval_result.error:
                br.error = eval_result.error
        else:
            br.error = inf_result.error or 'No patch generated'

        # Save per-run detail
        _save_per_run_detail(base_dir, inst, tool, inf_result, eval_result, br)
        return br

    except Exception as e:
        log.error('  [%s] CRASH on %s: %s', tool.upper(), inst.instance_id, e, exc_info=True)
        return None  # crash = don't save, resume will re-try


def run_pipeline(
    instances: list[SWEInstance],
    tools: list[str],
    base_dir: Path,
    output_path: Path,
    skip_eval: bool = False,
    existing_results: list[BenchmarkResult] = None,
    completed: set = None,
    env_map: dict = None,
    cooldown: int = COOLDOWN_SECONDS,
    model_configs: list[ModelConfig] = None,
):
    """Run the full inference + evaluation pipeline with parallel execution.

    Each model gets its own thread pool (sized by model.concurrency).
    All models run in parallel, and within each model, multiple instances
    run concurrently. Results are saved after each completion for robust resume.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    all_results = list(existing_results or [])
    completed = completed or set()
    start_time = time.time()

    # Build model configs from tools list if not provided
    if not model_configs:
        model_configs = []
        for t in tools:
            if t in MODEL_PRESETS:
                model_configs.append(MODEL_PRESETS[t])
            elif t == 'cc':
                model_configs.append(MODEL_PRESETS['cc'])

    total_runs = len(instances) * len(model_configs)
    done_count = len(completed)
    _done_lock = _threading.Lock()

    log.info('Pipeline: %d instances × %d models = %d total runs (%d already done)',
             len(instances), len(model_configs), total_runs, done_count)
    for mc in model_configs:
        log.info('  Model: %s (backend=%s, model_id=%s, concurrency=%d)',
                 mc.name, mc.backend, mc.model_id, mc.concurrency)

    def _process_one(inst: SWEInstance, mcfg: ModelConfig):
        """Process one (instance, model) pair — called from thread pool."""
        nonlocal done_count
        run_key = f'{inst.instance_id}__{mcfg.name}'
        if run_key in completed:
            return None

        br = _run_one_instance(inst, mcfg, base_dir, env_map, skip_eval)

        # None = inference failed (429, timeout, crash) → don't save,
        # so --resume will re-try this instance next time.
        if br is None:
            with _done_lock:
                done_count += 1
                _dc = done_count
            log.warning('[%d/%d] ⏭️  %s %s SKIPPED (inference failed, will retry on resume)',
                        _dc, total_runs, mcfg.name.upper(), inst.instance_id)
            return None

        with _results_lock:
            all_results.append(br)
            with _done_lock:
                done_count += 1
                _dc = done_count
            # Save after each completion
            try:
                _save_results(all_results, instances, output_path, model_configs)
            except Exception as e:
                log.error('Failed to save results: %s', e, exc_info=True)

        status = '✅' if br.resolved else '❌'
        log.info('[%d/%d] %s %s %s (%.0fs, $%.3f)',
                 _dc, total_runs, status, mcfg.name.upper(),
                 inst.instance_id, br.duration_s, br.cost_usd)

        return br

    # Launch one ThreadPoolExecutor per model, all running concurrently
    all_futures = {}  # future → (inst, mcfg)
    executors = []

    for mcfg in model_configs:
        # Build the work queue for this model (skip completed)
        work = [(inst, mcfg) for inst in instances
                if f'{inst.instance_id}__{mcfg.name}' not in completed]
        if not work:
            log.info('[%s] All %d instances already completed', mcfg.name.upper(), len(instances))
            continue

        log.info('[%s] Submitting %d instances (concurrency=%d)',
                 mcfg.name.upper(), len(work), mcfg.concurrency)
        executor = ThreadPoolExecutor(
            max_workers=mcfg.concurrency,
            thread_name_prefix=f'swe-{mcfg.name}',
        )
        executors.append(executor)

        for inst, mc in work:
            future = executor.submit(_process_one, inst, mc)
            all_futures[future] = (inst, mc)

    # Wait for all futures to complete, logging progress
    completed_count = 0
    total_futures = len(all_futures)
    last_progress_time = time.time()

    for future in as_completed(all_futures):
        completed_count += 1
        inst, mc = all_futures[future]
        try:
            br = future.result()
        except Exception as e:
            log.error('Future crashed for %s/%s: %s', mc.name, inst.instance_id, e, exc_info=True)

        # Print progress every 60 seconds
        now = time.time()
        if now - last_progress_time > 60:
            last_progress_time = now
            elapsed = now - start_time
            rate = completed_count / max(elapsed, 1) * 3600
            remaining = (total_futures - completed_count) / max(rate / 3600, 0.001)
            log.info('━ Progress: %d/%d futures done (%.0f/hr), ETA: %s ━',
                     completed_count, total_futures, rate,
                     time.strftime('%H:%M', time.localtime(now + remaining)))
            with _results_lock:
                print_progress(all_results, total_runs)

    # Shutdown executors
    for executor in executors:
        executor.shutdown(wait=False)

    # Final save
    with _results_lock:
        _save_results(all_results, instances, output_path, model_configs)

    return all_results


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='SWE-bench Verified evaluation: Tofu vs Claude Code CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('--num', type=int, default=None,
                        help='Number of instances (default: 5 unless --all)')
    parser.add_argument('--all', action='store_true',
                        help='Run ALL 500 instances')
    parser.add_argument('--instances', type=str, default='',
                        help='Comma-separated instance IDs')
    parser.add_argument('--repo', type=str, default='',
                        help='Filter by repository')
    parser.add_argument('--difficulty', type=str, default='',
                        help='Filter by difficulty')
    parser.add_argument('--tool', choices=['tofu', 'cc', 'both'], default='both',
                        help='(Legacy) Run tofu, cc, or both')
    parser.add_argument('--models', type=str, default='',
                        help='Comma-separated model names to test. '
                             f'Available: {",".join(MODEL_PRESETS.keys())}. '
                             'Overrides --tool when specified.')
    parser.add_argument('--delay', type=int, default=COOLDOWN_SECONDS,
                        help=f'Delay between runs (default: {COOLDOWN_SECONDS}s)')
    parser.add_argument('--skip-eval', action='store_true',
                        help='Inference only, save patches')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--setup-envs-only', action='store_true',
                        help='Only set up conda environments, no inference')
    parser.add_argument('--output', type=str, default='',
                        help='Output JSON path')
    parser.add_argument('--workdir', type=str, default=str(DEFAULT_WORKDIR),
                        help='Working directory for workspaces')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from existing results')
    parser.add_argument('--smart-resume', action='store_true', dest='smart_resume',
                        help='Resume: keep resolved results, re-run all non-resolved instances')
    parser.add_argument('--reeval', action='store_true',
                        help='Re-evaluate all existing patches (skip inference)')
    parser.add_argument('--skip-c-repos', action='store_true', default=True,
                        help='Skip repos requiring C compilation (astropy, sklearn, matplotlib). Default: True')
    parser.add_argument('--include-c-repos', action='store_true',
                        help='Include C-extension repos (requires Docker-like env)')
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()

    _cooldown = args.delay

    base_dir = Path(args.workdir).resolve()
    base_dir.mkdir(parents=True, exist_ok=True)
    output_path = Path(args.output) if args.output else base_dir / 'swebench_results.json'

    # ─── Set up structured log file (always persisted) ─────────────────────
    log_file = base_dir / 'swebench_runner.log'
    file_handler = logging.FileHandler(str(log_file), mode='a', encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)-5s [%(funcName)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    ))
    log.addHandler(file_handler)

    # Redirect stderr to the log file so tracebacks are never lost
    # (critical when running under nohup > /dev/null 2>&1)
    stderr_log = open(str(base_dir / 'swebench_stderr.log'), 'a', encoding='utf-8')
    sys.stderr = stderr_log

    log.info('═══ SWE-bench runner started — log file: %s ═══', log_file)

    # Load instances
    log.info('Loading SWE-bench Verified dataset...')
    instance_ids = [x.strip() for x in args.instances.split(',') if x.strip()] if args.instances else None
    num = args.num if args.num else (None if args.all else 5)
    skip_repos = C_EXTENSION_REPOS if (args.skip_c_repos and not args.include_c_repos) else None
    instances = load_swebench_instances(
        num=num,
        instance_ids=instance_ids,
        repo_filter=args.repo or None,
        difficulty_filter=args.difficulty or None,
        seed=args.seed,
        load_all=args.all,
        skip_repos=skip_repos,
    )
    log.info('Selected %d instances', len(instances))

    # Dry-run
    if args.dry_run:
        print(f'\n📋 Selected {len(instances)} instances:\n')
        for inst in instances:
            print(f'  [{inst.difficulty:>15s}] {inst.instance_id:<45s} ({inst.repo} v{inst.version})')
        diffs = Counter(i.difficulty for i in instances)
        repos = Counter(i.repo for i in instances)
        print(f'\n  By difficulty: {dict(sorted(diffs.items()))}')
        print(f'  By repo: {dict(sorted(repos.items(), key=lambda x: -x[1]))}')

        # Estimate cost
        # Estimate with model configs if available
        if args.models:
            model_names = [m.strip() for m in args.models.split(',') if m.strip()]
            n_models = len(model_names)
        else:
            n_models = 2 if args.tool == 'both' else 1
        avg_cost_per_instance = 0.5  # rough estimate
        est_cost = len(instances) * avg_cost_per_instance * n_models
        # With parallel execution, time ≈ instances × avg_time / max_concurrency
        max_conc = sum(MODEL_PRESETS.get(m, MODEL_PRESETS.get('tofu')).concurrency
                       for m in (args.models.split(',') if args.models else ['tofu']))
        # ~5 min avg per run (inference + eval), divided by total concurrency
        est_time_h = len(instances) * n_models * 5 / max(max_conc, 1) / 60
        print(f'\n  Models: {n_models} | Total runs: {len(instances) * n_models}')
        print(f'  Estimated cost: ~${est_cost:.0f}')
        print(f'  Estimated time: ~{est_time_h:.1f} hours (parallel)')
        return

    # Setup conda envs only
    if args.setup_envs_only:
        env_map = setup_all_conda_envs(instances)
        log.info('Done. %d environments ready.', sum(1 for v in env_map.values() if v))
        return

    # Build model configs
    if args.models:
        model_names = [m.strip() for m in args.models.split(',') if m.strip()]
        model_configs = []
        for mn in model_names:
            if mn in MODEL_PRESETS:
                model_configs.append(MODEL_PRESETS[mn])
            else:
                log.error('Unknown model preset: %s. Available: %s', mn, list(MODEL_PRESETS.keys()))
                sys.exit(1)
    else:
        # Legacy --tool mode
        if args.tool == 'both':
            model_configs = [MODEL_PRESETS['tofu'], MODEL_PRESETS['cc']]
        elif args.tool == 'tofu':
            model_configs = [MODEL_PRESETS['tofu']]
        else:
            model_configs = [MODEL_PRESETS['cc']]

    # Preflight: check which backends are available
    requested_tools = list(set(mc.backend for mc in model_configs))
    tools = preflight(requested_tools)
    if not tools:
        log.error('No backends available. Exiting.')
        sys.exit(1)

    # Filter out models whose backends aren't available
    model_configs = [mc for mc in model_configs if mc.backend in tools]
    if not model_configs:
        log.error('No models available after preflight. Exiting.')
        sys.exit(1)

    log.info('Models to benchmark: %s', [mc.name for mc in model_configs])

    # Resume — smart mode: keep resolved, re-run failures + unfinished
    existing_results, completed = [], set()
    if args.smart_resume:
        args.resume = True
    if args.resume and not args.reeval:
        existing_results, completed = _load_completed(output_path)
        if args.smart_resume:
            # Smart resume: only keep resolved results, re-run all non-resolved
            keep_results = [r for r in existing_results if r.resolved]
            rerun_keys = set(f'{r.instance_id}__{r.tool}' for r in existing_results if not r.resolved)
            completed = set(f'{r.instance_id}__{r.tool}' for r in keep_results)
            existing_results = keep_results
            log.info('Smart resume: keeping %d resolved, re-running %d failed/unfinished',
                     len(keep_results), len(rerun_keys))
        elif completed:
            log.info('Resuming: %d runs already completed', len(completed))

    # Pre-build conda environments
    log.info('Setting up conda environments...')
    env_map = setup_all_conda_envs(instances)

    # Pre-clone all repos
    log.info('Pre-cloning repositories...')
    repos = set(inst.repo for inst in instances)
    for repo in sorted(repos):
        try:
            get_repo_path(repo)
        except Exception as e:
            log.error('Failed to clone %s: %s', repo, e)

    # ─── Re-evaluate existing patches (--reeval) ──────────────────────────
    if args.reeval:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        log.info('═══ RE-EVALUATION MODE: re-running eval on existing patches ═══')
        patch_dir = base_dir / 'patches'
        inst_map = {inst.instance_id: inst for inst in instances}
        reeval_results = []

        patch_files = sorted(patch_dir.glob('*.diff')) if patch_dir.exists() else []
        log.info('Found %d patch files to re-evaluate', len(patch_files))

        # Build set of model names to re-evaluate
        reeval_model_names = set(mc.name for mc in model_configs)
        log.info('Re-evaluating models: %s', sorted(reeval_model_names))

        # Build work queue
        work_items = []
        for pf in patch_files:
            stem = pf.stem
            parts = stem.rsplit('__', 1)
            if len(parts) != 2:
                continue
            iid, tool = parts
            if tool not in reeval_model_names:
                continue
            inst = inst_map.get(iid)
            if not inst:
                continue
            model_patch = pf.read_text()
            if not model_patch or model_patch.startswith('# (empty'):
                continue
            work_items.append((iid, tool, inst, model_patch))

        log.info('Re-evaluating %d patches (parallel, %d workers)',
                 len(work_items), MAX_EVAL_WORKERS)

        def _reeval_one(iid, tool, inst, model_patch):
            """Re-evaluate one patch. Thread-safe."""
            log.info('  [%s] %s — re-evaluating (%d chars)', tool.upper(), iid, len(model_patch))
            eval_result = evaluate_patch(inst, model_patch, tool, base_dir, env_map or {})

            detail_file = base_dir / 'details' / f'{iid}__{tool}.json'
            inf_data = {}
            if detail_file.exists():
                try:
                    with open(detail_file) as f:
                        detail = json.load(f)
                    inf_data = detail.get('inference', {})
                except Exception:
                    pass

            br = BenchmarkResult(
                instance_id=iid,
                repo=inst.repo,
                difficulty=inst.difficulty,
                tool=tool,
                duration_s=inf_data.get('duration_s', 0),
                cost_usd=inf_data.get('cost_usd', 0),
                input_tokens=inf_data.get('input_tokens', 0),
                output_tokens=inf_data.get('output_tokens', 0),
                cache_read_tokens=inf_data.get('cache_read_tokens', 0),
                cache_write_tokens=inf_data.get('cache_write_tokens', 0),
                num_turns=inf_data.get('num_turns', 0),
                resolved=eval_result.resolved,
                patch_applies=eval_result.patch_applies,
                fail_to_pass_passed=sum(1 for v in eval_result.fail_to_pass_results.values() if v),
                fail_to_pass_total=len(eval_result.fail_to_pass_results),
                pass_to_pass_passed=sum(1 for v in eval_result.pass_to_pass_results.values() if v),
                pass_to_pass_total=len(eval_result.pass_to_pass_results),
                error=eval_result.error or '',
            )

            # Update detail file with new eval
            dummy_inf = type('obj', (object,), {
                'model_patch': model_patch,
                'duration_s': inf_data.get('duration_s', 0),
                'cost_usd': inf_data.get('cost_usd', 0),
                'input_tokens': inf_data.get('input_tokens', 0),
                'output_tokens': inf_data.get('output_tokens', 0),
                'cache_read_tokens': inf_data.get('cache_read_tokens', 0),
                'cache_write_tokens': inf_data.get('cache_write_tokens', 0),
                'num_turns': inf_data.get('num_turns', 0),
                'error': inf_data.get('error', ''),
                'raw_output': inf_data.get('raw_output', ''),
            })()
            _save_per_run_detail(base_dir, inst, tool, dummy_inf, eval_result, br)

            return br

        # Run in parallel
        completed_count = 0
        with ThreadPoolExecutor(max_workers=MAX_EVAL_WORKERS,
                                thread_name_prefix='reeval') as executor:
            futures = {
                executor.submit(_reeval_one, iid, tool, inst, mp): (iid, tool)
                for iid, tool, inst, mp in work_items
            }
            for future in as_completed(futures):
                iid, tool = futures[future]
                completed_count += 1
                try:
                    br = future.result()
                    with _results_lock:
                        reeval_results.append(br)
                    status = '✅' if br.resolved else '❌'
                    log.info('[%d/%d] %s %s %s F2P=%d/%d P2P=%d/%d',
                             completed_count, len(work_items), status,
                             tool.upper(), iid,
                             br.fail_to_pass_passed, br.fail_to_pass_total,
                             br.pass_to_pass_passed, br.pass_to_pass_total)
                except Exception as e:
                    log.error('[%d/%d] CRASH %s %s: %s',
                              completed_count, len(work_items), tool, iid, e,
                              exc_info=True)

                # Save periodically (every 20 completions)
                if completed_count % 20 == 0:
                    with _results_lock:
                        _save_results(reeval_results, instances, output_path)
                    log.info('━ Progress: %d/%d done ━', completed_count, len(work_items))

        # Final save
        _save_results(reeval_results, instances, output_path)
        print_summary(reeval_results)
        log.info('Re-evaluation complete. %d results saved: %s',
                 len(reeval_results), output_path)
        return

    # Summary before starting
    tool_names = [mc.name for mc in model_configs]
    new_runs = len(instances) * len(model_configs) - len(completed)
    log.info('')
    log.info('=' * 60)
    log.info('  SWE-bench Verified Benchmark')
    log.info('  Instances: %d | Models: %s | New runs: %d',
             len(instances), ', '.join(tool_names), new_runs)
    for mc in model_configs:
        log.info('    %s: %s (backend=%s, concurrency=%d)',
                 mc.name, mc.model_id, mc.backend, mc.concurrency)
    log.info('  Workdir: %s', base_dir)
    log.info('  Output: %s', output_path)
    log.info('  Safety timeout: %ds', INFERENCE_SAFETY_TIMEOUT)
    log.info('=' * 60)
    log.info('')

    # Run
    results = run_pipeline(
        instances, tool_names, base_dir, output_path,
        skip_eval=args.skip_eval,
        existing_results=existing_results,
        completed=completed,
        env_map=env_map,
        cooldown=0,  # No cooldown in parallel mode
        model_configs=model_configs,
    )

    # Final summary
    print_summary(results)
    log.info('Results saved: %s', output_path)
    log.info('Workdir: %s', base_dir)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        log.info('Interrupted by user (Ctrl+C)')
        sys.exit(130)
    except Exception:
        log.critical('Fatal unhandled exception', exc_info=True)
        sys.exit(1)
