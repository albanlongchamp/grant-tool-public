"""Append-only investigator memory, explicitly selected for each proposal.

Confirmation records an investigator action; it does not establish empirical
truth. Only current, confirmed, not-due versions can enter a new generation.
Unrelated catalog edits do not invalidate a selected version.
"""
from __future__ import annotations

import copy
from datetime import date, datetime, timezone
import json
import re
import uuid


CATEGORIES = {'capability', 'constraint', 'writing_preference', 'correction'}
STATUSES = {'proposed', 'confirmed', 'retired'}
ACTORS = {'investigator', 'assistant'}
_ID = re.compile(r'^[a-f0-9]{32}$')


def _now():
    return datetime.now(timezone.utc).isoformat()


def _today():
    # User-entered "Review on" dates follow this desktop's local calendar.
    return date.today()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _conflict(message):
    from .studio import Conflict
    raise Conflict(message)


def initialize(c):
    c.executescript('''
    CREATE TABLE IF NOT EXISTS memory_catalog (
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
      updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS memory_versions (
      version_id TEXT PRIMARY KEY, entry_id TEXT NOT NULL, version INTEGER NOT NULL,
      category TEXT NOT NULL, text TEXT NOT NULL, scope TEXT NOT NULL, tags TEXT NOT NULL,
      provenance_note TEXT NOT NULL, review_after TEXT NOT NULL, status TEXT NOT NULL,
      actor TEXT NOT NULL, created_at TEXT NOT NULL, confirmed_at TEXT,
      catalog_revision INTEGER NOT NULL, UNIQUE(entry_id,version));
    CREATE INDEX IF NOT EXISTS memory_entry_versions ON memory_versions(entry_id,version DESC);
    CREATE TABLE IF NOT EXISTS proposal_memory_bindings (
      proposal_id TEXT PRIMARY KEY REFERENCES proposals(id), catalog_revision INTEGER NOT NULL,
      selections TEXT NOT NULL, selected_at TEXT NOT NULL);
    ''')
    c.execute('INSERT OR IGNORE INTO memory_catalog VALUES (1,1,?)', (_now(),))


def _record(row):
    item = dict(row)
    item['id'] = item.pop('entry_id')
    item['tags'] = json.loads(item['tags'])
    return item


def _head(c, entry_id):
    row = c.execute('SELECT * FROM memory_versions WHERE entry_id=? ORDER BY version DESC LIMIT 1',
                    (entry_id,)).fetchone()
    return _record(row) if row else None


def _catalog_revision(c):
    return c.execute('SELECT revision FROM memory_catalog WHERE singleton=1').fetchone()[0]


def catalog(c):
    row = c.execute('SELECT revision,updated_at FROM memory_catalog WHERE singleton=1').fetchone()
    grouped = {}
    for version in c.execute('SELECT * FROM memory_versions ORDER BY entry_id,version DESC'):
        item = _record(version)
        grouped.setdefault(item['id'], []).append(item)
    entries = [{**copy.deepcopy(history[0]), 'history': copy.deepcopy(history)}
               for history in grouped.values()]
    entries.sort(key=lambda item: (item['created_at'], item['id']), reverse=True)
    return {'revision': row['revision'], 'updated_at': row['updated_at'], 'entries': entries}


def _expected(c, revision):
    if type(revision) is not int or revision != _catalog_revision(c):
        _conflict('Research memory changed. Reload the memory catalog before saving or selecting records.')


def _identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError('Select a valid research-memory entry or version.')
    return value


def _string(data, field, maximum, *, required=True):
    value = data.get(field, '')
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f'{field} must be {"nonempty " if required else ""}text of at most {maximum} characters.')
    return value.strip()


def _clean(data, actor):
    if actor not in ACTORS:
        raise ValueError('Memory changes must identify the investigator or assistant actor.')
    allowed = {'category', 'text', 'scope', 'tags', 'provenance_note', 'review_after', 'status'}
    if not isinstance(data, dict) or set(data) - allowed:
        raise ValueError('Use only the supported research-memory fields.')
    category, status = data.get('category'), data.get('status', 'proposed')
    if category not in CATEGORIES or status not in STATUSES:
        raise ValueError('Choose a supported memory category and status.')
    if actor != 'investigator' and status != 'proposed':
        raise ValueError('Only an explicit investigator action can confirm or retire research memory.')
    item = {'category': category, 'status': status,
            'text': _string(data, 'text', 3000), 'scope': _string(data, 'scope', 1200),
            'provenance_note': _string(data, 'provenance_note', 2400)}
    tags = data.get('tags', [])
    if (not isinstance(tags, list) or len(tags) > 12 or
            any(not isinstance(tag, str) or not tag.strip() or len(tag) > 120 for tag in tags)):
        raise ValueError('Use at most 12 nonempty tags of at most 120 characters each.')
    item['tags'] = list(dict.fromkeys(tag.strip() for tag in tags))
    review_after = _string(data, 'review_after', 10, required=False)
    if review_after:
        if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', review_after):
            raise ValueError('Review on must be an ISO date in YYYY-MM-DD format.')
        date.fromisoformat(review_after)
    item['review_after'] = review_after
    return item


def _append(c, entry_id, version, data, actor):
    stamp = _now()
    revision = _catalog_revision(c) + 1
    c.execute('UPDATE memory_catalog SET revision=?,updated_at=? WHERE singleton=1', (revision, stamp))
    c.execute('INSERT INTO memory_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (uuid.uuid4().hex, entry_id, version, data['category'], data['text'], data['scope'],
               _json(data['tags']), data['provenance_note'], data['review_after'], data['status'],
               actor, stamp, stamp if data['status'] == 'confirmed' else None, revision))


def create(store, expected_catalog_revision, data, actor='assistant'):
    data = _clean(data, actor)
    with store.connect(True) as c:
        _expected(c, expected_catalog_revision)
        _append(c, uuid.uuid4().hex, 1, data, actor)
        result = catalog(c)
    return result


def revise(store, entry_id, expected_catalog_revision, data, actor='assistant'):
    entry_id = _identifier(entry_id)
    data = _clean(data, actor)
    with store.connect(True) as c:
        _expected(c, expected_catalog_revision)
        current = _head(c, entry_id)
        if current is None:
            raise KeyError('Research-memory entry not found.')
        _append(c, entry_id, current['version'] + 1, data, actor)
        result = catalog(c)
    return result


def _state(current, selected):
    if current is None:
        return 'missing', 'The selected memory entry is unavailable. Remove or replace its proposal selection.'
    if current['status'] == 'retired':
        return 'retired', 'The selected memory entry was retired. Remove it or select a newly investigator-confirmed version.'
    if current['status'] != 'confirmed':
        return 'proposed', 'The latest memory version is proposed. Investigator confirmation and reselection are required.'
    if current['review_after'] and date.fromisoformat(current['review_after']) <= _today():
        return 'review_due', 'This memory entry is due for review. The investigator must review a new version and you must reselect it.'
    if current['version_id'] != selected.get('version_id'):
        return 'stale', 'This proposal selected an older memory version. Review and explicitly select the current confirmed version.'
    if selected.get('status') != 'confirmed' or selected.get('actor') != 'investigator':
        return 'proposed', 'The selected snapshot is not investigator-confirmed. Review and reselect a confirmed version.'
    return 'current', 'The exact selected version is current and investigator-confirmed.'


def _selection(c, value, catalog_revision, stamp):
    if not isinstance(value, dict) or set(value) - {'entry_id', 'version_id', 'note'}:
        raise ValueError('A selection must contain entry_id, version_id and an optional note.')
    entry_id, version_id = _identifier(value.get('entry_id')), _identifier(value.get('version_id'))
    note = _string(value, 'note', 2400, required=False)
    current = _head(c, entry_id)
    if current is None:
        raise KeyError('Research-memory entry not found.')
    chosen = {**current, 'version_id': version_id}
    state, message = _state(current, chosen)
    if state != 'current':
        _conflict(message)
    return {**copy.deepcopy(current), 'entry_id': entry_id, 'note': note, 'selected_at': stamp,
            'version_catalog_revision': current['catalog_revision'], 'catalog_revision': catalog_revision}


def select(store, proposal_id, expected_revision, expected_catalog_revision, selections):
    if not isinstance(selections, list) or len(selections) > 12:
        raise ValueError('Explicitly select at most 12 research-memory entries for a proposal.')
    with store.connect(True) as c:
        store._editable(c, proposal_id, expected_revision)
        _expected(c, expected_catalog_revision)
        stamp = _now()
        frozen = [_selection(c, item, expected_catalog_revision, stamp) for item in selections]
        if len({item['entry_id'] for item in frozen}) != len(frozen):
            raise ValueError('Select each research-memory entry only once.')
        previous = c.execute('SELECT selections FROM proposal_memory_bindings WHERE proposal_id=?',
                             (proposal_id,)).fetchone()
        previous = json.loads(previous[0]) if previous else []
        key = lambda items: [(item['entry_id'], item['version_id'], item['note']) for item in items]
        if key(previous) != key(frozen):
            c.execute('INSERT INTO proposal_memory_bindings VALUES (?,?,?,?) '
                      'ON CONFLICT(proposal_id) DO UPDATE SET catalog_revision=excluded.catalog_revision, '
                      'selections=excluded.selections,selected_at=excluded.selected_at',
                      (proposal_id, expected_catalog_revision, _json(frozen), stamp))
            c.execute('UPDATE proposals SET context_id=NULL WHERE id=?', (proposal_id,))
            store._bump(c, proposal_id)
    return store.get(proposal_id)


def _selected(c, proposal_id):
    row = c.execute('SELECT selections FROM proposal_memory_bindings WHERE proposal_id=?',
                    (proposal_id,)).fetchone()
    return json.loads(row[0]) if row else []


def bindings(c, proposal_id):
    result = []
    for item in _selected(c, proposal_id):
        current = _head(c, item['entry_id'])
        state, message = _state(current, item)
        result.append({**item, 'current_state': state, 'state_message': message,
                       'current_version_id': current['version_id'] if current else None})
    return result


def validate_snapshot(c, snapshots):
    if not isinstance(snapshots, list) or len(snapshots) > 12:
        _conflict('The queued research-memory selection is invalid. Reselect memory and start a new task.')
    seen = set()
    for item in snapshots:
        if not isinstance(item, dict) or not isinstance(item.get('entry_id'), str):
            _conflict('The queued research-memory snapshot is incomplete. Reselect memory and start a new task.')
        if item['entry_id'] in seen:
            _conflict('The queued research-memory selection contains duplicate entries. Reselect memory.')
        seen.add(item['entry_id'])
        current = _head(c, item['entry_id'])
        state, message = _state(current, item)
        if state != 'current':
            _conflict(message + ' Start a new task after updating the selection.')
        # Bind text, scope and provenance to the immutable version, not only its ID.
        for key, value in current.items():
            if key != 'catalog_revision' and item.get(key) != value:
                _conflict('The queued memory snapshot differs from its recorded version. Reselect memory and start a new task.')
    return None


def snapshot(c, proposal_id):
    selected = _selected(c, proposal_id)
    validate_snapshot(c, selected)
    return copy.deepcopy(selected)


def source(selected):
    """Convert an already validated frozen selection into an attributed source."""
    if selected.get('status') != 'confirmed' or selected.get('actor') != 'investigator' or not selected.get('confirmed_at'):
        raise ValueError('Only an investigator-confirmed memory snapshot can become a generation source.')
    return {
        'id': 'M_' + selected['version_id'], 'layer': 'investigator_memory',
        'title': 'Research memory — ' + selected['category'].replace('_', ' ') + f" (version {selected['version']})",
        'text': selected['text'], 'source_uri': 'urn:grant-memory:' + selected['entry_id'] + ':' + selected['version_id'],
        'metadata': {'role': 'investigator_statement', 'evidence_status': 'investigator_confirmed',
                     'status': 'investigator_confirmed', 'entry_id': selected['entry_id'],
                     'version_id': selected['version_id'], 'version': selected['version'],
                     'category': selected['category'], 'scope': selected['scope'],
                     'tags': copy.deepcopy(selected['tags']), 'provenance_note': selected['provenance_note'],
                     'confirmed_at': selected['confirmed_at'], 'confirmation_date': selected['confirmed_at'][:10],
                     'review_after': selected['review_after'], 'actor': selected['actor'],
                     'selected_at': selected['selected_at'], 'selection_note': selected['note'],
                     'locator': 'Investigator-confirmed memory version ' + str(selected['version']),
                     'limitations': ['Recorded investigator confirmation, not independent verification of a scientific finding.']},
    }
