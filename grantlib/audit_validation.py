"""Local audit-item quarantine; exact location is not scientific verification."""
from copy import deepcopy
import hashlib
import json


ERROR_CODES = {'invalid_anchor', 'item_rule_violation',
               'unresolved_commitment_link_unavailable'}
ERROR_LABELS = {
    'invalid_anchor': 'an exact input anchor was invalid',
    'item_rule_violation': 'an item routing or required-anchor rule failed',
    'unresolved_commitment_link_unavailable': 'an unresolved commitment lacked an available explanation',
}
PARTIAL_LIMITATION = (
    'Some assessment items failed local validation and were excluded in full. '
    'Only accepted items are shown; excluded items are not resolved or disproved. '
    'This partial assessment cannot establish that no scientific issues remain.')


class ItemError(ValueError):
    def __init__(self, code, message):
        if code not in ERROR_CODES:
            raise ValueError('Unsupported audit validation code.')
        self.code = code
        super().__init__(message)


def partition(output, collections, validate_item):
    """Called only after the complete schema and identifier checks pass."""
    accepted, excluded = {}, []
    for collection in collections:
        accepted[collection] = []
        for position, raw in enumerate(output[collection]):
            item = deepcopy(raw)
            try:
                validate_item(item, collection)
            except ItemError as exc:
                excluded.append({'collection': collection, 'position': position,
                                 'error_code': exc.code})
            else:
                accepted[collection].append((position, item))
    return accepted, excluded


def finish(report, raw, excluded, limitation):
    """Attach safe diagnostics; partial model summary/limitations never escape."""
    accepted_counts = {key: len(report.get(key, [])) for key in ('findings', 'commitments')}
    excluded_counts = {key: sum(item['collection'] == key for item in excluded)
                       for key in accepted_counts}
    partial = bool(excluded)
    report['validation'] = {
        'status': 'partial' if partial else 'complete',
        'raw_report_sha256': hashlib.sha256(json.dumps(
            raw, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest(),
        'hash_scope': 'canonical_report_json',
        'accepted_counts': accepted_counts, 'excluded_counts': excluded_counts,
        'excluded_items': deepcopy(excluded),
    }
    if partial:
        report['summary'] = (
            f"Partial assessment: {accepted_counts['findings']} finding(s) and "
            f"{accepted_counts['commitments']} commitment(s) passed local validation; "
            f"{excluded_counts['findings']} finding(s) and "
            f"{excluded_counts['commitments']} commitment(s) were excluded. "
            'Exact location checks do not verify scientific meaning.')
        report['limitations'] = [limitation, PARTIAL_LIMITATION]
        report['state'] = 'issues_found' if report.get('findings') else 'unassessed'
    return report


def validation_summary(validation):
    """Render only bounded counts and fixed reasons, never model item text."""
    def count(group, key):
        values = validation.get(group) or {}
        value = values.get(key) if isinstance(values, dict) else None
        return str(value) if type(value) is int and 0 <= value <= 40 else 'unavailable'
    text = ('Accepted: ' + count('accepted_counts', 'findings') + ' finding(s), ' +
            count('accepted_counts', 'commitments') + ' commitment(s). Excluded: ' +
            count('excluded_counts', 'findings') + ' finding(s), ' +
            count('excluded_counts', 'commitments') + ' commitment(s).')
    items = validation.get('excluded_items')
    if isinstance(items, list):
        reasons = []
        for code, label in ERROR_LABELS.items():
            total = sum(isinstance(item, dict) and item.get('error_code') == code for item in items[:80])
            if total:
                reasons.append(label + ' (' + str(total) + ')')
        if reasons:
            text += ' Reasons: ' + '; '.join(reasons) + '.'
    return text


def export_history(check):
    """Safe partial-stage history separate from the currently displayed report."""
    stages = {'check-initial': 'Initial check', 'check-final': 'Final check'}
    steps = check.get('steps') or []
    if not isinstance(steps, list):
        return ''
    partial = []
    for index, step in enumerate(steps[:4]):
        if not isinstance(step, dict) or step.get('stage') not in stages:
            continue
        report = step.get('report') or {}
        validation = step.get('validation') or (report.get('validation') if isinstance(report, dict) else None)
        if isinstance(validation, dict) and validation.get('status') == 'partial':
            partial.append((index, step['stage'], validation))
    current = (check.get('report') or {}).get('validation') or {}
    current_hash = current.get('raw_report_sha256')
    current_index = None
    if current.get('status') == 'partial' and isinstance(current_hash, str) and len(current_hash) == 64:
        current_index = next((index for index, _, validation in reversed(partial)
                              if validation.get('raw_report_sha256') == current_hash), None)
    history = [(stage, validation) for index, stage, validation in partial if index != current_index]
    if not history:
        return ''
    lines = ['### Partial-check history', '',
             'These counts describe earlier check stages, separately from the current assessment. '
             'Historical findings and excluded item text are not reproduced.', '']
    for stage, validation in history:
        lines += ['- ' + stages[stage] + ': ' + validation_summary(validation)]
    return '\n'.join(lines) + '\n'
