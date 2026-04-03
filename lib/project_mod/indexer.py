"""AI-powered file indexing and context generation for project co-pilot."""
import hashlib
import json
import os
import re
import threading
import time

import lib as _lib  # module ref for hot-reload
from lib.llm_dispatch import smart_chat as llm_chat
from lib.log import get_logger
from lib.project_mod.config import (
    CODE_EXTENSIONS,
    IGNORE_DIRS,
    INDEX_DIR,
    INDEX_MODEL,
    LARGE_FILE_THRESHOLD,
    MAX_INDEX_FILE_SIZE,
    PARALLEL_INDEX_THRESHOLD,
    SKIP_INDEX_THRESHOLD,
    _lock,
    _state,
    rate_limiter,
)
from lib.project_mod.scanner import (
    _fmt_size,
    _is_data_file,
    _is_likely_data_content,
    _should_ignore,
)

logger = get_logger(__name__)

def _index_hash(path):
    return hashlib.md5(path.encode()).hexdigest()[:12]


def _file_hash(fp):
    """Compute a quick hash for file content change detection."""
    try:
        sz = os.path.getsize(fp)
        mtime = os.path.getmtime(fp)
        # Fast hash: size + mtime + first/last 1KB
        with open(fp, 'rb') as f:
            head = f.read(1024)
            f.seek(max(0, sz - 1024))
            tail = f.read(1024)
        data = f'{sz}:{mtime}:{head.hex()}:{tail.hex()}'
        return hashlib.md5(data.encode()).hexdigest()[:16]
    except Exception as e:
        logger.debug('[Indexer] file hash computation failed for %s: %s', fp, e, exc_info=True)
        return None


def _index_file_path(base_path):
    os.makedirs(INDEX_DIR, exist_ok=True)
    return os.path.join(INDEX_DIR, f'{_index_hash(base_path)}.json')


def _load_cached_index(base_path):
    fp = _index_file_path(base_path)
    try:
        if os.path.exists(fp):
            with open(fp) as f:
                data = json.load(f)
            # ★ Validate projectPath matches to prevent cross-path contamination
            cached_project = data.get('projectPath', '')
            # NOTE: if cached_project is empty/missing, the cache is legacy
            # and we cannot trust it — discard to be safe.
            if not cached_project or os.path.abspath(cached_project) != os.path.abspath(base_path):
                logger.debug('Index cache MISMATCH: cached for %s, '
                      'expected %s — discarding stale cache', cached_project, base_path)
                try:
                    os.remove(fp)
                except OSError:
                    logger.debug('[Indexer] stale cache removal failed for %s', fp)
                return None
            # ★ Self-healing: remove any entries whose paths are not under base_path.
            # This cleans up caches that were contaminated before the per-root fix.
            files = data.get('files', {})
            hashes = data.get('fileHashes', {})
            abs_base = os.path.abspath(base_path)
            bad_keys = []
            for rel in files:
                full = os.path.normpath(os.path.join(abs_base, rel))
                if not full.startswith(abs_base + os.sep) and full != abs_base:
                    bad_keys.append(rel)
            if bad_keys:
                logger.warning('Index self-heal: removing %d foreign entries '
                               'from cache for %s: %s...', len(bad_keys), base_path, bad_keys[:5])
                for k in bad_keys:
                    files.pop(k, None)
                    hashes.pop(k, None)
                # Re-save the cleaned cache immediately
                _save_index(base_path, data)

            age_h = (time.time() - data.get('createdAt', 0)) / 3600
            logger.info('Loaded cached index: '
                  '%d files, %.1fh old '
                  '(path: %s)', len(files), age_h, cached_project)
            return data
    except Exception as e:
        logger.error('Index load error: %s', e, exc_info=True)
    return None


def _save_index(base_path, index_data):
    # Always ensure projectPath is stamped before writing
    index_data['projectPath'] = os.path.abspath(base_path)
    fp = _index_file_path(base_path)
    try:
        with open(fp, 'w') as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        logger.info('Index saved: %s', fp)
        return True
    except Exception as e:
        logger.error('Index save error: %s', e, exc_info=True)
        return False


def start_indexing(base_path=None, model=INDEX_MODEL):
    with _lock:
        if _state['indexing']:
            return {'status': 'already_running', 'progress': _state['indexProgress']}
        if _state['scanning']:
            return {'status': 'error', 'message': 'Wait for scan to finish first'}
        path = base_path or _state['path']
        if not path:
            return {'status': 'error', 'message': 'No project set'}

        # ★ Check if project is too large for semantic indexing
        fc = _state.get('fileCount', 0)
        if fc > SKIP_INDEX_THRESHOLD:
            logger.info('Skipping semantic index: %d files > threshold %d. '
                  'Model will use tool-based exploration instead.', fc, SKIP_INDEX_THRESHOLD)
            return {
                'status': 'skipped',
                'message': (f'Project has {fc} files (threshold: {SKIP_INDEX_THRESHOLD}). '
                            f'Semantic indexing skipped — the AI will use grep_search, '
                            f'find_files, and list_dir to explore the project dynamically. '
                            f'This avoids RPM limits and is more efficient for large codebases.'),
                'fileCount': fc,
                'threshold': SKIP_INDEX_THRESHOLD,
            }

        _state['indexing'] = True
        _state['indexProgress'] = 'Starting…'
    threading.Thread(target=_index_worker, args=(path, model), daemon=True).start()
    return {'status': 'started'}


def _select_model_for_file(sz, ext):
    """Select model based on file size and type for optimal speed/cost."""
    # Small/medium files → Gemini Flash Lite (fast, cheap)
    if sz < LARGE_FILE_THRESHOLD:
        return _lib.GEMINI_MODEL
    # Large files or code files → Qwen (better understanding)
    if ext in CODE_EXTENSIONS:
        return _lib.QWEN_MODEL
    return _lib.GEMINI_MODEL


def _index_worker(base_path, model):
    try:
        # ★ BUG FIX: Guard — abort if project path changed before we even start
        with _lock:
            if _state['path'] != base_path:
                logger.info('Index worker aborted: path changed to %s '
                      '(was indexing %s)', _state['path'], base_path)
                _state['indexing'] = False
                return

        # ★ Load existing cached index for incremental indexing
        cached = _load_cached_index(base_path)
        cached_files = cached.get('files', {}) if cached else {}
        cached_hashes = cached.get('fileHashes', {}) if cached else {}
        logger.info('Cached index: %d files from previous run', len(cached_files))

        # ★ Categorize files by size for smart model selection
        files_small = []   # <20KB → Gemini Flash Lite (fast)
        files_medium = []  # 20-50KB → Gemini Flash Lite
        files_large = []   # >50KB → Qwen
        data_files_skipped = 0
        unchanged_preserved = 0
        files_to_index = []  # Track files that need indexing

        # ★ BUG FIX: Collect all current file descriptions (unchanged + data + new)
        # Previously, unchanged files were `continue`-d and lost from the final index.
        all_desc = {}       # Will hold ALL file descriptions (moved up from below)
        all_hashes = dict(cached_hashes)  # Start with cached hashes

        for root, dirs, files in os.walk(base_path):
            dirs[:] = [d for d in sorted(dirs)
                       if d not in IGNORE_DIRS and not d.startswith('.')]
            for fname in sorted(files):
                if _should_ignore(fname):
                    continue
                fp = os.path.join(root, fname)
                try:
                    sz = os.path.getsize(fp)
                except Exception as e:
                    logger.debug('[Indexer] stat failed for %s: %s', fp, e, exc_info=True)
                    continue
                if sz == 0 or sz > MAX_INDEX_FILE_SIZE:
                    continue
                rel = os.path.relpath(fp, base_path)
                ext = os.path.splitext(fname)[1].lower()

                # ★ Skip data files — give them a fixed description instead
                if _is_data_file(fname, sz):
                    desc = cached_files.get(rel) or f'Data file ({ext}, {_fmt_size(sz)})'
                    all_desc[rel] = desc
                    data_files_skipped += 1
                    continue

                # ★ Incremental check: skip if file hasn't changed
                curr_hash = _file_hash(fp)
                if rel in cached_files and cached_hashes.get(rel) == curr_hash:
                    # ★ BUG FIX: Preserve cached description for unchanged files
                    all_desc[rel] = cached_files[rel]
                    all_hashes[rel] = curr_hash
                    unchanged_preserved += 1
                    continue

                entry = (rel, fp, sz, ext, curr_hash)
                files_to_index.append(entry)
                if sz < 20_000:
                    files_small.append(entry)
                elif sz < LARGE_FILE_THRESHOLD:
                    files_medium.append(entry)
                else:
                    files_large.append(entry)

        total = len(files_small) + len(files_medium) + len(files_large)
        skipped_unchanged = unchanged_preserved
        logger.info('[Index] Walk complete: %d to index, %d unchanged preserved, '
                    '%d data files, %d pre-populated in all_desc',
                    total, unchanged_preserved, data_files_skipped, len(all_desc))
        with _lock:
            _state['indexProgress'] = f'0/{total} files…'

        # ★ Decide strategy: multi-model round-robin or single-model
        use_multi = total > PARALLEL_INDEX_THRESHOLD
        if use_multi:
            logger.info('Round-robin multi-model indexing %d files '
                  '(skipped %d data files, %d unchanged)', total, data_files_skipped, skipped_unchanged)
            logger.info('RPM limits: %s', rate_limiter.stats())
        else:
            logger.info('Indexing %d files with %s '
                  '(skipped %d data files, %d unchanged)', total, model, data_files_skipped, skipped_unchanged)

        # ★ Adaptive batch size: larger for small files, smaller for large files
        BATCH_SMALL = 15   # Gemini Flash Lite — small files
        BATCH_MED = 10     # Gemini Flash Lite — medium files
        BATCH_LARGE = 8    # Qwen for large/complex files
        MAX_CHARS = 3000
        # NOTE: all_desc is already populated above with unchanged + data files

        def _prepare_batch_contents(batch):
            """Read file contents for a batch, returns list of (rel, text, model, hash)."""
            contents = []
            for rel, fp, sz, ext, curr_hash in batch:
                # ★ Check cache again (may have been indexed in previous interrupted run)
                if rel in cached_files and cached_hashes.get(rel) == curr_hash:
                    all_desc[rel] = cached_files[rel]
                    logger.info('Restore cached: %s', rel)
                    continue
                try:
                    with open(fp, errors='replace') as f:
                        lines = []
                        for _ in range(60):
                            ln = f.readline()
                            if not ln:
                                break
                            lines.append(ln)
                    text = ''.join(lines)
                    if len(text) > MAX_CHARS:
                        text = text[:MAX_CHARS] + '\n…'
                    if _is_likely_data_content(text):
                        all_desc[rel] = f'Data file ({ext}, {_fmt_size(sz)})'
                        continue
                    # Select model per file
                    model_for_file = _select_model_for_file(sz, ext) if use_multi else model
                    contents.append((rel, text, model_for_file, curr_hash))
                except Exception as e:
                    logger.debug('[Indexer] file read failed for %s, submitting as unreadable: %s', rel, e, exc_info=True)
                    model_for_file = _select_model_for_file(sz, ext) if use_multi else model
                    contents.append((rel, '(could not read)', model_for_file, curr_hash))
            return contents

        def _index_batch_with_model(contents, batch_model, bn, tb, model_label):
            """Call LLM for a single batch with a specific model, respecting RPM."""
            # ★ Acquire rate limiter slot before calling LLM
            acquired = rate_limiter.acquire(batch_model, timeout=300)
            if not acquired:
                logger.warning('Batch %d/%d (%s): rate limiter timeout, skipping', bn, tb, model_label)
                hashes = {rel: h for rel, text, h in contents}
                return {}, hashes

            prompt = (
                'Briefly describe each file\'s purpose (1 sentence max per file). '
                'Reply ONLY with valid JSON: {"path": "description", ...}\n\n')
            for rel, text, curr_hash in contents:
                prompt += f'=== {rel} ===\n{text}\n\n'
            hashes = {rel: h for rel, text, h in contents}
            try:
                resp_text = _call_llm(
                    [{'role': 'user', 'content': prompt}], batch_model)
                rate_limiter.on_success(batch_model)
                parsed = _extract_json(resp_text)
                if parsed:
                    logger.info('Batch %d/%d (%s): +%d files', bn, tb, model_label, len(parsed))
                    return parsed, hashes
            except Exception as e:
                err_str = str(e)
                if '429' in err_str or 'rate' in err_str.lower():
                    rate_limiter.on_rate_limited(batch_model)
                    logger.info('Batch %d/%d (%s): rate limited (429), '
                          'RPM auto-reduced. Retrying after backoff...', bn, tb, model_label)
                    # ★ Retry once after rate limit backoff
                    time.sleep(5)
                    try:
                        acquired = rate_limiter.acquire(batch_model, timeout=120)
                        if acquired:
                            resp_text = _call_llm(
                                [{'role': 'user', 'content': prompt}], batch_model)
                            rate_limiter.on_success(batch_model)
                            parsed = _extract_json(resp_text)
                            if parsed:
                                logger.info('Batch %d/%d (%s): retry OK, +%d files', bn, tb, model_label, len(parsed))
                                return parsed, hashes
                    except Exception as e2:
                        logger.error('Batch %d/%d (%s): retry also failed: %s', bn, tb, model_label, e2, exc_info=True)
                else:
                    logger.error('Batch %d (%s) error: %s', bn, model_label, e, exc_info=True)
            return {}, hashes

        if use_multi:
            # ═══════════════════════════════════════════════════════
            # ★ Round-Robin Multi-Model Indexing (no size-based tiers)
            #
            # Instead of sending all small files to one model (which blows
            # its RPM limit), we distribute ALL files across ALL models
            # equally, respecting each model's individual RPM.
            # ═══════════════════════════════════════════════════════
            from concurrent.futures import ThreadPoolExecutor, as_completed

            # Merge all files, shuffle for uniform distribution
            all_files = files_small + files_medium + files_large

            # ★ Round-robin assignment: cycle through models evenly
            available_models = [
                (_lib.GEMINI_MODEL,      BATCH_SMALL, 'Gemini'),
                (_lib.QWEN_MODEL,        BATCH_LARGE, 'Qwen'),
            ]

            # Build batches with round-robin model assignment
            all_batches = []
            model_idx = 0
            file_idx = 0

            while file_idx < len(all_files):
                mdl, batch_size, label = available_models[model_idx % len(available_models)]
                batch = all_files[file_idx:file_idx + batch_size]
                file_idx += batch_size
                model_idx += 1

                contents = _prepare_batch_contents(batch)
                if contents:
                    # All files in this batch go to the assigned model (no sub-grouping)
                    stripped = [(rel, text, curr_hash) for rel, text, _mdl, curr_hash in contents]
                    all_batches.append((stripped, len(all_batches) + 1, mdl, label))

            tb = len(all_batches)
            with _lock:
                _state['indexProgress'] = f'0/{tb} batches (round-robin multi-model)…'

            # Log distribution
            from collections import Counter
            dist = Counter(label for _, _, _, label in all_batches)
            logger.info('Batch distribution: %s '
                  '(total %d batches for %d files)', dict(dist), tb, total)

            all_hashes = dict(cached_hashes)
            futures = {}

            # ★ max_workers=4 (conservative: won't overwhelm any single model)
            with ThreadPoolExecutor(max_workers=4) as executor:
                for contents, bn, mdl, label in all_batches:
                    future = executor.submit(
                        _index_batch_with_model, contents, mdl, bn, tb, label)
                    futures[future] = bn

                done_count = 0
                for future in as_completed(futures):
                    parsed, hashes = future.result()
                    if parsed:
                        all_desc.update(parsed)
                    all_hashes.update(hashes)
                    done_count += 1
                    with _lock:
                        _state['indexProgress'] = (
                            f'Batch {done_count}/{tb} · {len(all_desc)}/{total} files (round-robin)')
        else:
            # ★ Sequential indexing with single model (original behavior for small projects)
            all_hashes = dict(cached_hashes)
            batch_size = BATCH_LARGE
            for bs in range(0, total, batch_size):
                batch = (files_small + files_medium + files_large)[bs:bs + batch_size]
                bn = bs // batch_size + 1
                tb = (total + batch_size - 1) // batch_size
                with _lock:
                    _state['indexProgress'] = (
                        f'Batch {bn}/{tb} · {len(all_desc)}/{total} files')

                contents = _prepare_batch_contents(batch)
                if not contents:
                    continue

                stripped = [(rel, text, curr_hash) for rel, text, _mdl, curr_hash in contents]
                parsed, hashes = _index_batch_with_model(stripped, model, bn, tb, model.split('-')[0])
                if parsed:
                    all_desc.update(parsed)
                all_hashes.update(hashes)
                time.sleep(0.3)

        # Determine which strategy was used for the model label
        if use_multi:
            model_label = f'{_lib.GEMINI_MODEL}+{_lib.QWEN_MODEL} (round-robin)'
        else:
            model_label = model
        index_data = {
            'projectPath': base_path, 'createdAt': time.time(),
            'model': model_label, 'fileCount': total,
            'indexedCount': len(all_desc), 'files': all_desc,
            'fileHashes': all_hashes,
        }
        saved = _save_index(base_path, index_data)
        with _lock:
            # ★ BUG FIX: Guard — don't overwrite state if project changed during indexing
            if _state['path'] != base_path:
                logger.info('Index complete but path changed '
                      '(%s → %s), discarding result', base_path, _state['path'])
                _state['indexing'] = False
                return
            if saved:
                _state['index'] = index_data
                _state['indexing'] = False
                _state['indexProgress'] = f'Done: {len(all_desc)}/{total}'
                logger.info('Indexing complete: %d/%d', len(all_desc), total)
                logger.info('Rate limiter final stats: %s', rate_limiter.stats())
            else:
                _state['indexing'] = False
                _state['indexProgress'] = 'Error: Failed to save index file'
    except Exception as e:
        logger.error('Indexing error: %s', e, exc_info=True)
        with _lock:
            _state['indexing'] = False
            _state['indexProgress'] = f'Error: {e}'


def _call_llm(messages, model=None):
    """Call LLM for indexing — delegates to smart dispatcher (dual-key)."""
    content, _usage = llm_chat(
        messages=messages,
        model=model,
        max_tokens=4096,
        temperature=1,
        capability='cheap',
        timeout=120,
        log_prefix='[Indexer]',
    )
    return content


def _extract_json(text):
    if not text:
        return None
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception as e:
            logger.debug('[Indexer] JSON parse from markdown code block failed: %s', e, exc_info=True)
    depth = start = 0
    best = None
    for i, c in enumerate(text):
        if c == '{':
            if depth == 0:
                start = i
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    candidate = json.loads(text[start:i + 1])
                    if best is None or len(candidate) > len(best):
                        best = candidate
                except Exception as e:
                    logger.debug('[Indexer] JSON object extraction failed at depth=0: %s', e, exc_info=True)
    return best


# ═══════════════════════════════════════════════════════
#  ★ Context for Chat — with anti-redundancy instructions
# ═══════════════════════════════════════════════════════

def get_context_for_prompt(base_path=None):
    with _lock:
        path = base_path or _state['path']
        # ★ BUG FIX: _state is a global singleton shared by ALL conversations.
        #   If conv A has project /chatui and conv B has project /other,
        #   switching to conv B calls set_project("/other") which clobbers
        #   _state globally. When conv A's running task later calls
        #   get_context_for_prompt("/chatui"), it reads tree/stats from
        #   _state which now belongs to /other → cross-conversation interference!
        #
        #   Fix: only use _state metadata (tree, stats, index) when the
        #   requested path matches _state['path']. Otherwise provide a
        #   minimal tools-only context that still lets the LLM work.
        state_matches = (path == _state['path'])
        _state_path_snapshot = _state['path']  # for diagnostic logging outside lock
        if state_matches:
            tree = _state['tree']
            scanning = _state['scanning']
            fc, dc, ts = _state['fileCount'], _state['dirCount'], _state['totalSize']
            langs = _state['languages']
            index = _state['index']
        else:
            # ★ Path mismatch — another conversation has taken over _state.
            #   Check if this path exists as a named root.
            tree = None
            scanning = False
            fc = dc = ts = 0
            langs = {}
            index = None
            from lib.project_mod.config import _roots
            for _rn, _rs in _roots.items():
                if _rs.get('path') == path:
                    tree = _rs.get('tree')
                    scanning = _rs.get('scanning', False)
                    fc = _rs.get('fileCount', 0)
                    dc = _rs.get('dirCount', 0)
                    ts = _rs.get('totalSize', 0)
                    langs = _rs.get('languages', {})
                    index = _rs.get('index')
                    break
        # ★ Collect extra roots for multi-root workspace
        from lib.project_mod.config import _roots
        extra_roots = {}
        _roots_snapshot = {}  # full snapshot for prefix table
        for rn, rs in _roots.items():
            _roots_snapshot[rn] = rs.copy()
            if rs['path'] != path and rs.get('tree'):
                extra_roots[rn] = rs.copy()
    if not path:
        return None

    # ★ Diagnostic log: show what context is being built from
    if not state_matches and base_path:
        logger.info('[Context] Path mismatch: requested=%s, _state=%s — using fallback (tree=%s)',
                    base_path, _state_path_snapshot, 'found' if tree else 'missing')
    logger.debug('[Context] Building prompt for path=%s, index=%s, extra_roots=%s',
                 path, bool(index), list(extra_roots.keys()) if extra_roots else '[]')

    tools_section = """
Tools for code exploration:
- list_dir(path) — List directory contents
- read_files(reads) — Read one or more files/ranges in a single call (up to 20). Each entry: {path, start_line?, end_line?}
- grep_search(pattern, path?, include?) — Search patterns (regex) across files
- find_files(pattern, path?) — Find files by name glob

Tools for code modification:
- write_file(path, content, description?) — Write/create a file (overwrites entirely)
- apply_diff(path, search, replace, description?, replace_all?) — Apply targeted search-and-replace edit
  Set replace_all=true to replace ALL occurrences (default: errors on multiple matches for safety).
  For MULTIPLE edits, pass an 'edits' array: apply_diff(edits=[{path, search, replace, replace_all?, description?}, ...])
  Edits are applied sequentially so later edits see earlier changes. Much faster than separate calls.
- run_command(command, timeout?, working_dir?) — Execute shell command. In multi-root workspaces, use working_dir='rootname:' to run in a specific root.

Token-saving tools (use these to avoid re-generating existing content):
- emit_to_user(tool_round, comment) — TERMINAL: end your turn by pointing the user to a tool result they can already see, instead of re-outputting it.
  Use when a tool's raw output fully answers the question (e.g. command output, file contents, search results).
  The user sees all tool results in expandable panels. Just add a brief comment — do NOT repeat the output.
  This is a TERMINAL tool — calling it ends your turn immediately. Do NOT call other tools after this.
- write_file supports content_ref={tool_round, start?, end?} INSTEAD of content — to write a previous tool result to a file without regenerating it.
  Example: write_file(path="output.txt", content_ref={"tool_round": 3}) writes round 3's output to the file.
  Use content_ref whenever you need to save/copy content that already exists as a tool result from an earlier round.

Strategy:
1. Review the file tree above (if present) to understand structure; for large projects start with list_dir('.') and find_files()
2. Use grep_search to locate relevant code
3. Use read_files to examine files — batch multiple paths/ranges into ONE call to minimize round-trips
4. Provide answers with specific file paths and line numbers
5. When suggesting changes, show exact code with file path
6. Use apply_diff for small targeted edits, write_file for new files or major rewrites
7. When making multiple edits, prefer batch apply_diff(edits=[...]) over separate calls — this dramatically reduces round trips

⚠️ IMPORTANT — read WIDE, not narrow:
- When reading a function or class, read 200+ lines in one shot — don't read 50-line fragments and come back for more
- Prefer reading the WHOLE file (omit start_line/end_line) for files under 500 lines
- The server auto-expands to whole-file for files under ~40KB regardless of range, so don't worry about requesting too much

"""

    # ★ Tree still building — provide partial context, tools still work
    if scanning or not tree:
        return (f"[PROJECT CO-PILOT MODE]\n"
                f"Project: {path}\n"
                f"Status: {'Scanning… ' + str(fc) + ' files found so far' if scanning else 'Ready'}\n"
                f"\nFile tree is still being generated. "
                f"Use the tools below to explore.\n"
                f"{tools_section}\n"
                f"Start with list_dir('.') to see the project root.\n")

    from .config import SMALL_PROJECT_THRESHOLD

    is_small = fc <= SMALL_PROJECT_THRESHOLD

    ctx = (f"[PROJECT CO-PILOT MODE]\n"
           f"Project: {path}\n")

    # ── File Tree: small projects get full tree; large projects skip ──
    if is_small:
        ctx += f"\nFile Tree:\n{tree}\n"
    else:
        ctx += '\n'

    # ── AI Descriptions: small projects get all; large projects skip ──
    if index and index.get('files'):
        if is_small:
            ctx += '\nAI-Generated File Descriptions:\n'
            for fp, desc in sorted(index['files'].items()):
                ctx += f'  • {fp} — {desc}\n'

    # ═══════════════════════════════════════════════════════
    #  ★ Multi-Root: append extra workspace roots
    # ═══════════════════════════════════════════════════════
    if extra_roots:
        # Derive the primary root's display name for the prefix table
        primary_name = None
        for _rn, _rs in _roots_snapshot.items():
            if _rs.get('path') == path:
                primary_name = _rn
                break
        primary_name = primary_name or os.path.basename(path)

        ctx += f"\n{'='*50}\n"
        ctx += f"⚠️ MULTI-ROOT WORKSPACE — {1 + len(extra_roots)} roots active\n"
        ctx += f"{'='*50}\n"
        ctx += (
            f"MANDATORY: When this workspace has multiple roots, you MUST use the\n"
            f"'rootname:path' prefix for ALL file operations targeting non-primary roots.\n"
            f"Without the prefix, paths resolve under the PRIMARY root ({primary_name}).\n\n"
            f"Root prefix table:\n"
            f"  {primary_name}: → {path} (PRIMARY — default when no prefix)\n"
        )
        for rn, rs in extra_roots.items():
            ctx += f"  {rn}: → {rs['path']}\n"
        ctx += (
            f"\nExamples:\n"
            f"  read_files([{{path: '{primary_name}:src/main.py'}}])   — explicit primary\n"
        )
        first_extra = next(iter(extra_roots))
        ctx += (
            f"  read_files([{{path: '{first_extra}:src/main.py'}}])   — explicit extra root\n"
            f"  write_file(path='{first_extra}:config.yaml', ...)     — write to extra root\n"
            f"  run_command(command='npm test', working_dir='{first_extra}:')  — run in extra root\n"
            f"  grep_search(pattern='TODO', path='{first_extra}:src') — search in extra root\n"
        )
        ctx += "\n"
        for rn, rs in extra_roots.items():
            r_fc = rs.get('fileCount', 0)
            ctx += f"[{rn}] {rs['path']}\n"
            # Show tree for small extra roots
            r_tree = rs.get('tree')
            if r_tree and r_fc <= 80:
                ctx += "  File Tree:\n"
                for line in r_tree.split('\n'):
                    if line.strip():
                        ctx += f"    {line}\n"
            ctx += '\n'

    # ═══════════════════════════════════════════════════════
    #  ★ CLAUDE.md / Project Intelligence auto-detection
    # ═══════════════════════════════════════════════════════
    #  Automatically detect and inject project-level instruction files
    #  (CLAUDE.md, .cursorrules, AGENTS.md, COPILOT.md, etc.) into the
    #  system prompt. This replaces the old "read CLAUDE.md at session
    #  start" skill — now it's built-in and automatic.
    _INTELLIGENCE_FILES = ['CLAUDE.md', '.cursorrules', 'AGENTS.md', 'COPILOT.md']
    for intel_name in _INTELLIGENCE_FILES:
        intel_path = os.path.join(path, intel_name)
        if os.path.isfile(intel_path):
            try:
                with open(intel_path, encoding='utf-8', errors='replace') as f:
                    intel_content = f.read(32_000)  # Cap at 32KB to avoid bloating context
                if intel_content.strip():
                    ctx += (f"\n{'='*50}\n"
                            f"📋 Project Intelligence — {intel_name}\n"
                            f"{'='*50}\n"
                            f"(Auto-detected from {intel_path})\n"
                            f"⚠️ MANDATORY: All code changes in this project MUST comply with the rules below.\n\n"
                            f"{intel_content.strip()}\n")
                    logger.info('[Context] Injected project intelligence file: %s (%d chars)',
                                intel_path, len(intel_content))
            except OSError as e:
                logger.warning('[Context] Failed to read project intelligence file %s: %s',
                               intel_path, e)

    ctx += tools_section
    return ctx
