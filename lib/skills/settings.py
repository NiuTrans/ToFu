"""Authenticated settings projections; runtime visibility stays in registry.

Entry: skill_library. Dependencies: owner-aware paths and the shared scanner.
Absolute source locations belong only to this explicit file-management UI;
model-facing discovery continues to expose opaque skill:// references.
"""

from __future__ import annotations

import os

from lib.memory.storage import PROJECT_SKILLS_SUBDIR
from lib.skills.paths import owner_global_skills_dir
from lib.skills.registry import list_skills


def skill_library(project_path: str | None, *, owner_user_id: int,
                  include_shadowed: bool = False) -> dict:
    rows = list_skills(project_path, owner_user_id=owner_user_id,
                       include_shadowed=include_shadowed)
    for row in rows:
        row['source_path'] = row.pop('package_dir', '')
        row.pop('filepath', None)
        row.pop('body', None)  # Library cards never need full instruction pages.
        row['project_path'] = project_path if row['scope'] == 'project' else None
    return {
        'skills': rows,
        'destinations': {
            'global': owner_global_skills_dir(owner_user_id),
            'project': os.path.join(project_path, PROJECT_SKILLS_SUBDIR)
            if project_path else None,
        },
    }
