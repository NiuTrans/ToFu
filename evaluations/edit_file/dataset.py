"""Frozen cross-provider prompts for edit-tool selection evaluation."""

CASES = [
    {
        'id': 'insert_between_blocks',
        'kind': 'additive',
        'prompt': 'Insert the line B between the existing lines A and C.',
        'files': {'sample.txt': 'A\nC\n'},
        'expected': {'sample.txt': 'A\nB\nC\n'},
    },
    {
        'id': 'add_import',
        'kind': 'additive',
        'prompt': 'Add `import json` immediately after `import os`.',
        'files': {'app.py': 'import os\n\nVALUE = 1\n'},
        'expected': {'app.py': 'import os\nimport json\n\nVALUE = 1\n'},
    },
    {
        'id': 'replace_value',
        'kind': 'replacement',
        'prompt': 'Change TIMEOUT from 10 to 30.',
        'files': {'config.py': 'TIMEOUT = 10\n'},
        'expected': {'config.py': 'TIMEOUT = 30\n'},
    },
]

CASES_BY_ID = {case['id']: case for case in CASES}
