
import os, sys

# ── 配置 ──
PROJECT_ROOT = os.getcwd()
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

IGNORE_DIRS = {
    '__pycache__', '.git', '.venv', 'venv', 'env', 'node_modules',
    '.idea', '.vscode', '.mypy_cache', '.pytest_cache', 'dist', 'build',
    'egg-info', '.eggs', '.tox', 'debug'
}
IGNORE_FILES = {
    '.DS_Store', 'Thumbs.db', '.gitignore', '*.pyc', '*.pyo',
    '*.db', '*.sqlite3', '*.db-journal', '*.db-wal',
    'prompt_ask.py', 'prompt_read.py', 'ask.txt',
}
IGNORE_EXTENSIONS = {
    '.pyc', '.pyo', '.db', '.sqlite3', '.db-journal', '.db-wal',
    '.ico', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.woff', '.woff2',
    '.ttf', '.eot', '.mp4', '.mp3', '.zip', '.tar', '.gz',
}

FILE_DESCRIPTIONS = {
    'server.py':            'Flask 主服务器，路由定义，中间件，入口',
    'lib/__init__.py':      '配置常量（API Key、URL、模型名、抓取参数）',
    'lib/database.py':      'SQLite 数据库初始化、连接、CRUD',
    'lib/tools.py':         'LLM 工具定义（web_search、fetch_url）',
    'lib/fetch.py':         'URL 抓取、PDF 解析、HTML 文本提取',
    'lib/search.py':        'DuckDuckGo/Wikipedia 搜索引擎',
    'lib/tasks.py':         '任务引擎：LLM 流式调用、工具循环、预抓取',
    'index.html':           '前端 HTML 骨架，引用 CSS 和 JS',
    'static/styles.css':    '全部 CSS 样式',
    'static/js/core.js':    '前端状态管理、配置、Markdown 渲染、流缓冲区',
    'static/js/ui.js':      '侧栏、消息渲染、流式 UI、SSE/轮询连接',
    'static/js/main.js':    '输入处理、文件上传、设置面板、应用初始化',
    'requirements.txt':     'Python 依赖',
    'README.md':            '项目说明',
}


def should_ignore_dir(dirname):
    return dirname in IGNORE_DIRS or dirname.startswith('.')

def should_ignore_file(filename):
    if filename in IGNORE_FILES:
        return True
    _, ext = os.path.splitext(filename)
    if ext in IGNORE_EXTENSIONS:
        return True
    for pattern in IGNORE_FILES:
        if pattern.startswith('*') and filename.endswith(pattern[1:]):
            return True
    return False

def get_description(relpath):
    normalized = relpath.replace('\\', '/')
    if normalized in FILE_DESCRIPTIONS:
        return FILE_DESCRIPTIONS[normalized]
    _, ext = os.path.splitext(relpath)
    return {
        '.py': 'Python 模块', '.js': 'JavaScript 模块', '.css': '样式表',
        '.html': 'HTML 页面', '.json': '配置/数据文件', '.md': '文档',
        '.txt': '文本文件', '.sh': 'Shell 脚本',
        '.yml': 'YAML 配置', '.yaml': 'YAML 配置',
        '.toml': 'TOML 配置', '.cfg': '配置文件', '.ini': '配置文件',
    }.get(ext, '')

def scan_project(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted([d for d in dirnames if not should_ignore_dir(d)])
        for filename in sorted(filenames):
            if should_ignore_file(filename):
                continue
            fullpath = os.path.join(dirpath, filename)
            relpath = os.path.relpath(fullpath, root)
            desc = get_description(relpath)
            files.append({
                'path': relpath.replace('\\', '/'),
                'desc': desc,
            })
    return files

def build_tree(files):
    tree = {}
    for f in files:
        parts = f['path'].split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part + '/', {})
        node[parts[-1]] = f

    lines = []
    def render(node, prefix=''):
        items = sorted(node.items(), key=lambda x: (not isinstance(x[1], dict) or not x[0].endswith('/'), x[0]))
        for i, (name, value) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = '└── ' if is_last else '├── '
            if isinstance(value, dict) and name.endswith('/'):
                lines.append(f'{prefix}{connector}{name}')
                render(value, prefix + ('    ' if is_last else '│   '))
            else:
                desc = ''
                if isinstance(value, dict) and value.get('desc'):
                    desc = f"  ← {value['desc']}"
                lines.append(f"{prefix}{connector}{name}{desc}")
    render(tree)
    return '\n'.join(lines)

def generate_prompt(files):
    tree_str = build_tree(files)

    table_lines = ['| # | 文件路径 | 用途 |',
                   '|---|----------|------|']
    for i, f in enumerate(files, 1):
        desc = f['desc'] or '-'
        table_lines.append(f"| {i} | `{f['path']}` | {desc} |")

    return f"""# 项目概览

## 目录结构

```
{tree_str}
```

## 文件清单

{chr(10).join(table_lines)}

---

以上是我项目的完整结构。请根据我接下来描述的问题，告诉我你需要查看哪些文件的完整内容。

请用以下格式回复：

```
需要查看的文件:
- 文件路径1
- 文件路径2
```

如果需要额外上下文（报错日志、浏览器控制台输出等），也请一并说明。
"""


def main():
    files = scan_project(PROJECT_ROOT)
    if not files:
        print("未找到任何项目文件！", file=sys.stderr)
        sys.exit(1)

    prompt = generate_prompt(files)

    output_path = os.path.join(SCRIPT_DIR, 'ask.txt')
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(prompt)

    print(f"已保存到: {output_path}")
    print(f"共 {len(files)} 个文件")


if __name__ == '__main__':
    main()
