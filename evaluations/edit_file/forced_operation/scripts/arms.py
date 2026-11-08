"""Freeze the four arms (offered tool schema + system prompt) into tasks/arms.json.

Run inside the hermetic env with PYTHONPATH=<tofu worktree> (imports the live
production edit_file schema from lib.tools.project).

A1 tofu_replace   production edit_file, operation enum narrowed to ["replace"]
A2 tofu_insert    production edit_file, insert-first wording, all three operations
A3 cc_edit        Claude Code 2.1.207 `Edit` (old_string/new_string), text from its bundle
A4 codex_patch    Codex `apply_patch` patch language (vendored codex @ 5c5308fc9a9e),
                  wrapped as a JSON function {input: <patch>} because the providers here
                  have no OpenAI freeform/grammar tools
"""
import copy
import hashlib
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODEX = os.environ.get('EDITBENCH_CODEX_DIR', '')  # vendored codex checkout (read-only)
CC_STRINGS = os.environ.get('EDITBENCH_CC_STRINGS', '')  # strings dump of the CC 2.1.207 binary


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def must_replace(text, old, new):
    if text.count(old) != 1:
        raise SystemExit(f'production text drifted; expected exactly one {old!r}')
    return text.replace(old, new)


def tofu_arms():
    from lib.tools.project import PROJECT_TOOL_EDIT_FILE
    base = copy.deepcopy(PROJECT_TOOL_EDIT_FILE)
    production_sha = sha(json.dumps(base, sort_keys=True))
    desc = base['function']['description']
    desc = must_replace(desc, ' Append at end-of-file with write_file(append=true).', '')
    item = base['function']['parameters']['properties']['edits']['items']['properties']

    a1 = copy.deepcopy(base)
    a1['function']['description'] = must_replace(
        desc, ' For insert_after/insert_before, content is only new text; the anchor stays automatically.', '')
    p1 = a1['function']['parameters']['properties']['edits']['items']['properties']
    p1['operation']['enum'] = ['replace']
    p1['operation']['description'] = must_replace(
        item['operation']['description'],
        ' insert_after adds content below anchor; insert_before above it.', '')
    p1['content']['description'] = must_replace(
        item['content']['description'], ' Insert: added lines only, without copied anchor/context.', '')
    p1['replace_all']['description'] = 'Replace every match.'

    a2 = copy.deepcopy(base)
    d2 = must_replace(
        desc,
        'Prefer replace, including additions: content is the complete desired replacement, including '
        'retained anchor text and all line breaks. After A: `{"operation":"replace","anchor":"A",'
        '"content":"A\\nB"}`; before A: `{"operation":"replace","anchor":"A","content":"B\\nA"}`. '
        'For insert_after/insert_before, content is only new text; the anchor stays automatically.',
        'Prefer insert_after/insert_before for additions: content is only the new text; the anchor '
        'stays automatically. After A: `{"operation":"insert_after","anchor":"A","content":"B"}`; '
        'before A: `{"operation":"insert_before","anchor":"A","content":"B"}`. Use replace only to '
        'change or remove existing text: content is the complete desired replacement, including '
        'retained anchor text and all line breaks.')
    a2['function']['description'] = d2
    p2 = a2['function']['parameters']['properties']['edits']['items']['properties']
    p2['operation']['enum'] = ['insert_after', 'insert_before', 'replace']
    p2['operation']['description'] = (
        'insert_after adds content below anchor; insert_before above it. Use replace only for edits '
        'that change or remove existing text.')
    return a1, a2, production_sha


def cc_arm():
    with open(CC_STRINGS, encoding='utf-8', errors='replace') as f:
        bundle = f.read()
    start = bundle.index('return`Performs exact string replacements in files.') + len('return`')
    end = bundle.index('This parameter is useful if you want to rename a variable for instance.`', start)
    template = bundle[start:end + len('This parameter is useful if you want to rename a variable for instance.')]
    read_rule = ('\n- You must use your `Read` tool at least once in the conversation before editing. '
                 'This tool will error if you attempt an edit without reading the file.')
    unique_rule = ('\n- The edit will FAIL if `old_string` is not unique in the file. Either provide a larger '
                   'string with more surrounding context to make it unique or use `replace_all` to change '
                   'every instance of `old_string`.')
    if unique_rule.strip() not in bundle.replace('\\`', '`'):
        raise SystemExit('CC uniqueness rule not found in bundle')
    desc = (template.replace('${cTg()}', read_rule).replace('${r}', 'line number + tab')
            .replace('${n}', unique_rule).replace('\\`', '`'))
    if '${' in desc:
        raise SystemExit('unresolved CC template placeholder')
    schema = {'type': 'function', 'function': {
        'name': 'Edit', 'description': desc,
        'parameters': {'type': 'object', 'properties': {
            'file_path': {'type': 'string', 'description': 'The absolute path to the file to modify'},
            'old_string': {'type': 'string', 'description': 'The text to replace'},
            'new_string': {'type': 'string',
                           'description': 'The text to replace it with (must be different from old_string)'},
            'replace_all': {'type': 'boolean', 'default': False,
                            'description': 'Replace all occurrences of old_string (default false)'}},
            'required': ['file_path', 'old_string', 'new_string'], 'additionalProperties': False}}}
    return schema, sha(template)


def codex_arm():
    prompt_path = os.path.join(CODEX, 'codex-rs/core/gpt_5_1_prompt.md')
    lark_path = os.path.join(CODEX, 'codex-rs/core/assets/tools/apply_patch.lark')
    with open(prompt_path, encoding='utf-8') as f:
        prompt = f.read()
    with open(lark_path, encoding='utf-8') as f:
        lark = f.read()
    start = prompt.index('Use the `apply_patch` tool to edit files. Your patch language')
    end = prompt.index('## `update_plan`', start)
    section = prompt[start:end].strip()
    desc = ('The `apply_patch` tool can be used to edit files. Put the whole patch text in the '
            '`input` argument.\n\n' + section + '\n\nThe patch must match this Lark grammar:\n' + lark)
    schema = {'type': 'function', 'function': {
        'name': 'apply_patch', 'description': desc,
        'parameters': {'type': 'object', 'properties': {
            'input': {'type': 'string', 'description': 'The entire contents of the apply_patch command'}},
            'required': ['input'], 'additionalProperties': False}}}
    return schema, sha(section), sha(lark)


SYSTEM_BASE = (
    'You are a coding agent working in the repository at {root}. You change files only by calling '
    'the provided tool. The user has already shown you the current file contents; each line is '
    'prefixed with its line number and a tab, and that prefix is not part of the file. Perform '
    'exactly the requested edit, with exact text and indentation. When the tool reports that the '
    'edit succeeded, reply with the single word DONE. If the tool reports an error, fix the call '
    'and try again.')

STYLE = {
    'A1': ('Edit style for this session: call `edit_file` and use operation "replace" for every edit '
           'item, including pure additions (the anchor is the existing text and `content` is that '
           'text together with the new lines). Put all edits for a file in one call.'),
    'A2': ('Edit style for this session: call `edit_file`. For every pure addition of new lines use '
           'operation "insert_after" or "insert_before" with `content` containing only the new lines '
           '(never repeat the anchor). Use "replace" only where existing text must change or be '
           'removed. Put all edits for a file in one call.'),
    'A3': ('Edit style for this session: call `Edit` with old_string/new_string. Make one `Edit` call '
           'per edit location; you may issue several calls in one response.'),
    'A4': ('Edit style for this session: call `apply_patch` with a patch in the apply_patch format in '
           'the `input` argument. Put all edits for a file in one patch.'),
}


def main():
    a1, a2, production_sha = tofu_arms()
    a3, cc_sha = cc_arm()
    a4, codex_section_sha, lark_sha = codex_arm()
    arms = {
        'A1': {'name': 'tofu_replace', 'tool': a1, 'executor': 'tofu'},
        'A2': {'name': 'tofu_insert', 'tool': a2, 'executor': 'tofu'},
        'A3': {'name': 'cc_edit', 'tool': a3, 'executor': 'cc'},
        'A4': {'name': 'codex_patch', 'tool': a4, 'executor': 'codex'},
    }
    for key, arm in arms.items():
        arm['system'] = SYSTEM_BASE + '\n\n' + STYLE[key]
        arm['tool_sha256'] = sha(json.dumps(arm['tool'], sort_keys=True))
    meta = {'tofu_production_edit_file_sha256': production_sha, 'cc_template_sha256': cc_sha,
            'codex_prompt_section_sha256': codex_section_sha, 'codex_lark_sha256': lark_sha,
            'system_base': SYSTEM_BASE}
    out = os.path.join(ROOT, 'tasks', 'arms.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump({'meta': meta, 'arms': arms}, f, ensure_ascii=False, indent=1)
    for key, arm in arms.items():
        print(key, arm['name'], arm['tool_sha256'][:12], len(json.dumps(arm['tool'])), 'chars')
    print(json.dumps(meta, indent=1))


if __name__ == '__main__':
    sys.exit(main())
