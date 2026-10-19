"""Deterministic five-case repository corpus shared by current and historical collectors.

No runtime, optional-backend or Git-history imports occur while building inputs.
"""
CASES = ('python_extraction', 'multilingual_extraction', 'ranking', 'incremental_edit', 'cold_scan')
OWNER = 900001


def fixture(case):
    files = {}
    count = 96 if case != 'ranking' else 192
    languages = ('py',) if case == 'python_extraction' else ('py', 'js', 'ts', 'rs')
    for i in range(count):
        previous = (i - 1) % count
        sources = {
            'py': f'def symbol_{i}():\n    return symbol_{previous}()\n',
            'js': f'export function symbol_{i}() {{ return symbol_{previous}(); }}\n',
            'ts': f'export function symbol_{i}(): number {{ return symbol_{previous}(); }}\n',
            'rs': f'fn symbol_{i}() -> usize {{ symbol_{previous}() }}\n',
        }
        language = languages[i % len(languages)]
        files[f'module_{i}.{language}'] = sources[language]
    return files

