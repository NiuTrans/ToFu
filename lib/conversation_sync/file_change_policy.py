"""Undo capability for canonical file changes; shared by projection and commands.

Local task history owns undo. External project receipts expose committed facts
but do not grant the local history gateway authority over a remote filesystem.
"""


def local_undo_available(files, *, truncated=False):
    from lib.project_mod.receipts import is_host_file_change
    return bool(files) and not truncated and all(is_host_file_change(row) for row in files)
