import os, sys, re, argparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.getcwd()
OUTPUT_FILE = os.path.join(SCRIPT_DIR, 'prompt_output.txt')

IGNORE_DIRS = {
    '__pycache__', '.git', '.venv', 'venv', 'env', 'node_modules',
    '.idea', '.vscode', '.mypy_cache', '.pytest_cache', 'debug'
}
IGNORE_FILES = {'.DS_Store', 'Thumbs.db'}
IGNORE_EXTENSIONS = {
    '.pyc', '.pyo', '.db', '.sqlite3', '.db-journal', '.db-wal',
    '.ico', '.png', '.jpg', '.jpeg', '.gif', '.woff', '.woff2',
    '.ttf', '.eot', '.mp4', '.mp3', '.zip', '.tar', '.gz',
}
SELF_SCRIPTS = {'prompt_ask.py', 'prompt_read.py'}

def scan_all_files(root):
    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith('.')]
        for filename in sorted(filenames):
            if filename in IGNORE_FILES or filename in SELF_SCRIPTS:
                continue
            _, ext = os.path.splitext(filename)
            if ext in IGNORE_EXTENSIONS:
                continue
            fullpath = os.path.join(dirpath, filename)
            relpath = os.path.relpath(fullpath, root).replace('\\', '/')
            files.append(relpath)
    return files

def build_tree_str(file_paths):
    tree = {}
    for fp in sorted(file_paths):
        parts = fp.split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part + '/', {})
        node[parts[-1]] = None

    lines = []
    def render(node, prefix=''):
        items = sorted(node.items(), key=lambda x: (x[1] is None, x[0]))
        for i, (name, value) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = '└── ' if is_last else '├── '
            marker = ' ◄' if value is None else ''
            lines.append(f'{prefix}{connector}{name}{marker}')
            if value is not None:
                render(value, prefix + ('    ' if is_last else '│   '))
    render(tree)
    return '\n'.join(lines)

def extract_paths_from_text(text):
    paths = []
    for line in text.splitlines():
        line = line.strip()
        m = re.match(r'^[-*•]\s*`?([^`\s]+\.\w+)`?', line)
        if m:
            paths.append(m.group(1))
            continue
        m = re.match(r'^\d+[.)]\s*`?([^`\s]+\.\w+)`?', line)
        if m:
            paths.append(m.group(1))
    return paths

def fuzzy_match(query, all_files):
    query_lower = query.lower().replace('\\', '/')
    for f in all_files:
        if f == query_lower or f.lower() == query_lower:
            return f
    for f in all_files:
        if f.lower().endswith('/' + query_lower) or f.lower() == query_lower:
            return f
    for f in all_files:
        basename = os.path.basename(f)
        name_no_ext = os.path.splitext(basename)[0]
        if name_no_ext.lower() == query_lower or basename.lower() == query_lower:
            return f
    matches = [f for f in all_files if query_lower in f.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return min(matches, key=len)
    return None

def read_file_content(root, relpath):
    fullpath = os.path.join(root, relpath)
    try:
        with open(fullpath, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read()
        return content
    except FileNotFoundError:
        return None
    except Exception as e:
        return f'[读取失败: {e}]'

def get_lang(path):
    ext_map = {
        '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
        '.html': 'html', '.css': 'css', '.json': 'json',
        '.yml': 'yaml', '.yaml': 'yaml', '.md': 'markdown',
        '.sh': 'bash', '.sql': 'sql', '.toml': 'toml',
        '.xml': 'xml', '.txt': '', '.cfg': '', '.ini': 'ini',
    }
    _, ext = os.path.splitext(path)
    return ext_map.get(ext, '')

def generate_prompt(root, selected_paths, all_files, bug_description=None):
    parts = []
    parts.append('# 项目调试 — 文件内容\n')
    parts.append('## 项目结构（◄ = 下方已包含完整内容）\n')

    marked_tree = build_tree_for_selected(all_files, selected_paths)
    parts.append(f'```\n{marked_tree}\n```\n')

    if bug_description:
        parts.append(f'## 问题描述\n\n{bug_description}\n')

    parts.append(f'## 文件内容（共 {len(selected_paths)} 个文件）\n')

    for relpath in selected_paths:
        content = read_file_content(root, relpath)
        lang = get_lang(relpath)
        if content is None:
            parts.append(f'### `{relpath}` — ⚠️ 文件不存在\n')
        else:
            parts.append(f'### `{relpath}`\n')
            parts.append(f'```{lang}\n{content}\n```\n')

    parts.append('---')
    return '\n'.join(parts)

def build_tree_for_selected(all_files, selected):
    selected_set = set(selected)
    tree = {}
    for fp in sorted(all_files):
        parts = fp.split('/')
        node = tree
        for part in parts[:-1]:
            node = node.setdefault(part + '/', {})
        node[parts[-1]] = fp in selected_set

    lines = []
    def render(node, prefix=''):
        items = sorted(node.items(), key=lambda x: (isinstance(x[1], bool), x[0]))
        for i, (name, value) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = '└── ' if is_last else '├── '
            if isinstance(value, bool):
                marker = ' ◄' if value else ''
                lines.append(f'{prefix}{connector}{name}{marker}')
            else:
                lines.append(f'{prefix}{connector}{name}')
                render(value, prefix + ('    ' if is_last else '│   '))
    render(tree)
    return '\n'.join(lines)

def main():
    parser = argparse.ArgumentParser(
        description='整合指定文件内容生成调试 prompt，保存到脚本同目录下 prompt_output.txt',
        epilog='示例:\n'
               '  python prompt_read.py server.py lib/tasks.py\n'
               '  python prompt_read.py tasks ui core --bug "流式输出闪烁"\n'
               '  pbpaste | python prompt_read.py --stdin\n'
               '  python prompt_read.py --all',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('files', nargs='*', help='文件路径（支持模糊匹配）')
    parser.add_argument('--stdin', action='store_true', help='从 stdin 读取大模型回复，自动提取文件路径')
    parser.add_argument('--list', '-l', type=str, help='从文件读取路径列表（每行一个）')
    parser.add_argument('--all', '-a', action='store_true', help='包含所有文件（谨慎！）')
    parser.add_argument('--bug', '-b', type=str, help='Bug/问题描述')
    parser.add_argument('--bug-file', '-f', type=str, help='从文件读取 bug 描述')
    parser.add_argument('--root', '-r', type=str, default=PROJECT_ROOT, help='项目根目录')
    parser.add_argument('--max-size', type=int, default=500, help='单文件最大 KB，超过跳过（默认 500）')
    parser.add_argument('--output', '-o', type=str, default=OUTPUT_FILE, help=f'输出文件路径（默认 {OUTPUT_FILE}）')
    args = parser.parse_args()

    root = args.root
    all_files = scan_all_files(root)

    if not all_files:
        print("未找到任何项目文件！", file=sys.stderr)
        sys.exit(1)

    requested = []

    if args.all:
        requested = list(all_files)
    else:
        if args.stdin:
            text = sys.stdin.read()
            extracted = extract_paths_from_text(text)
            if extracted:
                print(f"从输入中提取到 {len(extracted)} 个文件路径", file=sys.stderr)
                requested.extend(extracted)
            else:
                print("未能从输入中提取到文件路径，请检查格式", file=sys.stderr)
                sys.exit(1)

        if args.list:
            with open(args.list, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#'):
                        requested.append(line)

        if args.files:
            requested.extend(args.files)

    if not requested:
        parser.print_help()
        print("\n请指定要查看的文件，或使用 --stdin / --all", file=sys.stderr)
        sys.exit(1)

    resolved = []
    not_found = []
    for req in requested:
        matched = fuzzy_match(req, all_files)
        if matched:
            if matched not in resolved:
                resolved.append(matched)
        else:
            not_found.append(req)

    if not_found:
        print(f"⚠️  未找到以下文件:", file=sys.stderr)
        for nf in not_found:
            print(f"   - {nf}", file=sys.stderr)
        print(file=sys.stderr)

    if not resolved:
        print("没有匹配到任何文件！", file=sys.stderr)
        print(f"\n可用文件:", file=sys.stderr)
        for f in all_files:
            print(f"  {f}", file=sys.stderr)
        sys.exit(1)

    skipped = []
    final = []
    for f in resolved:
        fullpath = os.path.join(root, f)
        try:
            size_kb = os.path.getsize(fullpath) / 1024
            if size_kb > args.max_size:
                skipped.append((f, size_kb))
            else:
                final.append(f)
        except:
            final.append(f)

    if skipped:
        print(f"⚠️  以下文件超过 {args.max_size}KB，已跳过:", file=sys.stderr)
        for f, kb in skipped:
            print(f"   - {f} ({kb:.0f}KB)", file=sys.stderr)
        print(f"   使用 --max-size 调整限制", file=sys.stderr)
        print(file=sys.stderr)

    bug = args.bug
    if args.bug_file:
        with open(args.bug_file, 'r', encoding='utf-8') as f:
            bug = f.read().strip()

    print(f"📦 包含 {len(final)} 个文件:", file=sys.stderr)
    for f in final:
        print(f"   ✓ {f}", file=sys.stderr)
    print(file=sys.stderr)

    prompt = generate_prompt(root, final, all_files, bug)

    # 保存到脚本同目录下的文本文件
    output_path = args.output
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(prompt)

    print(f"✅ 已保存到: {output_path}", file=sys.stderr)
    print(f"   共 {len(prompt)} 字符", file=sys.stderr)

if __name__ == '__main__':
    main()
