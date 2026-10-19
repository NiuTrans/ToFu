"""Common current-source assessment for task consumers and owner review evidence.

Pure projection over the working-fact graph. Readers supply their observed
revision range; current provenance never establishes correctness or authority.
"""


def assess_dependency(fact, first_revision, last_revision):
    if fact is None or fact['status'] == 'unavailable':
        return 'unavailable', ['source_unavailable']
    if (first_revision, last_revision) != (fact['revision'], fact['revision']):
        return 'changed', ['fact_revision_changed']
    if fact['status'] in {'stale', 'withdrawn'}:
        return 'changed', ['source_dependency_changed']
    if fact['status'] == 'needs_review':
        return 'needs_review', ['source_needs_review']
    return 'current', []
