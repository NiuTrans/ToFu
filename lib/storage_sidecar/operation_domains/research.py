"""Paper, research, report, and daily-cost operation registrations."""

from lib.storage_sidecar import operations as ops


OPERATIONS = {
    'research.artifact.upsert': ops.OperationSpec(
        'command', True, ops._research_artifact_upsert),
    'research.artifacts.get': ops.OperationSpec(
        'query', False, ops._research_artifacts_get),
    'research.directions.list': ops.OperationSpec(
        'query', False, ops._research_directions_list),
    'paper.report.upsert': ops.OperationSpec('command', True, ops._paper_report_upsert),
    'paper.report.get': ops.OperationSpec('query', False, ops._paper_report_get),
    'paper.report.latest': ops.OperationSpec(
        'query', False, ops._paper_report_latest),
    'paper.report.second_pass.merge': ops.OperationSpec(
        'command', True, ops._paper_report_second_pass_merge),
    'paper.report.second_pass.accumulate': ops.OperationSpec(
        'command', True, ops._paper_report_second_pass_accumulate),
    'paper.translation.upsert': ops.OperationSpec(
        'command', True, ops._paper_translation_upsert),
    'paper.translation.get': ops.OperationSpec(
        'query', False, ops._paper_translation_get),
    'paper.library.put': ops.OperationSpec('command', True, ops._paper_library_put),
    'paper.library.delete': ops.OperationSpec(
        'command', True, ops._paper_library_delete),
    'paper.library.recent': ops.OperationSpec(
        'query', False, ops._paper_library_recent),
    'paper.library.list': ops.OperationSpec(
        'query', False, ops._paper_library_list),
    'paper.library.identity': ops.OperationSpec(
        'query', False, ops._paper_library_identity),
    'paper.library.title.backfill': ops.OperationSpec(
        'command', True, ops._paper_library_title_backfill),
    'paper.note.list': ops.OperationSpec('query', False, ops._paper_note_list),
    'paper.note.create': ops.OperationSpec('command', True, ops._paper_note_create),
    'paper.note.update': ops.OperationSpec('command', True, ops._paper_note_update),
    'paper.note.delete': ops.OperationSpec('command', True, ops._paper_note_delete),
    'daily_cost.month': ops.OperationSpec('query', False, ops._daily_cost_month),
    'daily_cost.upsert': ops.OperationSpec('command', True, ops._daily_cost_upsert),
    'daily_cost.delete': ops.OperationSpec('command', True, ops._daily_cost_delete),
    'daily_cost.persisted_dates': ops.OperationSpec(
        'query', False, ops._daily_cost_persisted_dates),
    'daily_cost.latest': ops.OperationSpec('query', False, ops._daily_cost_latest),
    'paper.podcast.upsert': ops.OperationSpec(
        'command', True, ops._paper_podcast_upsert),
    'paper.podcast.get': ops.OperationSpec('query', False, ops._paper_podcast_get),
    'paper.podcast.mark_interrupted': ops.OperationSpec(
        'command', True, ops._paper_podcast_mark_interrupted),
}

__all__ = ['OPERATIONS']
