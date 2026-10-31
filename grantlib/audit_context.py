"""Remove only designated historical automatic opinions from review context."""
from copy import deepcopy


HISTORICAL_CHECK_FIELDS = ('previous_draft_check', 'previous_revision_check')


def prepare(context):
    """Return an independent copy and JSON-pointer paths of excluded fields.

    Only direct fields on selected_version and related_versions entries are
    excluded. Scientific content, selected reviews, human judgments, evidence
    and similarly named fields elsewhere are preserved without transformation.
    """
    if not isinstance(context, dict):
        raise ValueError('An automatic audit requires a context object.')
    cleaned = deepcopy(context)
    excluded = []

    def remove(version, path):
        if isinstance(version, dict):
            # deepcopy preserves shared references. Replace each targeted
            # container so an evidence/review alias keeps its exact contents.
            version = version.copy()
            for field in HISTORICAL_CHECK_FIELDS:
                if field in version:
                    del version[field]
                    excluded.append(path + '/' + field)
        return version

    if 'selected_version' in cleaned:
        cleaned['selected_version'] = remove(cleaned['selected_version'], '/selected_version')
    related = cleaned.get('related_versions')
    if isinstance(related, list):
        cleaned['related_versions'] = [remove(version, f'/related_versions/{index}')
                                       for index, version in enumerate(related)]
    return cleaned, excluded
