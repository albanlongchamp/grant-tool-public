"""Workspace reference library built from the grant archive and proposal lookups.

The archive index already records every DOI and PMID observed in indexed
paragraphs and labels reference-list sections. This module turns those
observations into resolved bibliographic records shared across proposals,
remembers which archived documents cited each paper, and lets a proposal adopt
a library record without a new network request.

Only public identifiers, or reference-list entries that look like citations,
are sent to Crossref or PubMed. A fuzzy Crossref match is labelled as such with
its confidence; it is never presented as an explicit identifier. Resolution is
identity metadata, not verification of a scientific claim.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
import uuid

from . import claim_review, library, references

MATCH_THRESHOLD = 0.6
MAX_PAGE = 20
_ENTRY_SPLIT = re.compile(r'\n+(?=\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s)')
_LEADING_NUMBER = re.compile(r'^\s*(?:\[\d{1,3}\]|\(\d{1,3}\)|\d{1,3}[.)])\s+')
_YEAR = re.compile(r'\b(19[5-9]\d|20[0-4]\d)\b')
_WORD = re.compile(r'[a-z0-9]+')
_STOP = {'the', 'and', 'with', 'from', 'for', 'into', 'that', 'this', 'study', 'analysis', 'using', 'based', 'after', 'between'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def initialize(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS library_references (
      id TEXT PRIMARY KEY, identity_key TEXT NOT NULL UNIQUE, snapshot TEXT NOT NULL,
      identity_basis TEXT NOT NULL, match TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS library_citations (
      reference_id TEXT NOT NULL REFERENCES library_references(id), document_id TEXT NOT NULL,
      relative_path TEXT, project TEXT, title TEXT, role TEXT, cycle TEXT, locator TEXT NOT NULL,
      entry_text TEXT, recorded_at TEXT NOT NULL, PRIMARY KEY(reference_id, document_id, locator));
    CREATE TABLE IF NOT EXISTS library_proposal_links (
      reference_id TEXT NOT NULL REFERENCES library_references(id), proposal_id TEXT NOT NULL,
      reference_record_id TEXT NOT NULL, linked_at TEXT NOT NULL, PRIMARY KEY(reference_id, proposal_id));
    CREATE TABLE IF NOT EXISTS library_attempts (
      attempt_key TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT, attempted_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS reference_checks (
      identity_key TEXT PRIMARY KEY, checked_at TEXT NOT NULL, status TEXT NOT NULL,
      provider TEXT, flags TEXT NOT NULL, notices TEXT, previous_flags TEXT NOT NULL, changed INTEGER NOT NULL,
      resolved_pmid TEXT);
    ''')
    if 'resolved_pmid' not in {row[1] for row in connection.execute('PRAGMA table_info(reference_checks)')}:
        connection.execute('ALTER TABLE reference_checks ADD COLUMN resolved_pmid TEXT')


# ---------------------------------------------------------------- identity

def identity_key(snapshot):
    canonical = snapshot.get('canonical') or snapshot.get('identifiers') or {}
    if canonical.get('pmid'):
        return 'pmid:' + str(canonical['pmid'])
    if canonical.get('doi'):
        return 'doi:' + str(canonical['doi']).lower()
    return None


def _keys(snapshot):
    canonical = snapshot.get('canonical') or snapshot.get('identifiers') or {}
    return {k for k in ('pmid:' + str(canonical.get('pmid') or ''), 'doi:' + str(canonical.get('doi') or '').lower())
            if not k.endswith(':')}


def _find(connection, snapshot):
    """Existing library row sharing a PMID or DOI with the snapshot, if any."""
    for key in _keys(snapshot):
        row = connection.execute('SELECT * FROM library_references WHERE identity_key=?', (key,)).fetchone()
        if row:
            return row
    for row in connection.execute('SELECT * FROM library_references'):
        if _keys(json.loads(row['snapshot'])) & _keys(snapshot):
            return row
    return None


def remember(store, snapshot, *, identity_basis='explicit_identifier', match=None, proposal_link=None):
    """Upsert one resolved snapshot into the shared library; returns the library row as a dict."""
    if not isinstance(snapshot, dict) or snapshot.get('status') != 'resolved' or not snapshot.get('title'):
        raise ValueError('Only a resolved reference with a title can enter the library.')
    key = identity_key(snapshot)
    if not key:
        raise ValueError('The reference has no PMID or DOI to identify it in the library.')
    record = json.loads(_json(snapshot))
    record.pop('id', None)
    record['canonical'] = record.get('canonical') or record.get('identifiers') or {}
    stamp = _now()
    with store.connect(True) as c:
        existing = _find(c, record)
        if existing:
            library_id = existing['id']
            # Keep the first identity basis unless an explicit identifier upgrades a fuzzy match.
            basis = existing['identity_basis']
            if basis != 'explicit_identifier' and identity_basis == 'explicit_identifier':
                c.execute('UPDATE library_references SET snapshot=?, identity_basis=?, match=NULL, updated_at=? WHERE id=?',
                          (_json(record), identity_basis, stamp, library_id))
        else:
            if c.execute('SELECT COUNT(*) FROM library_references').fetchone()[0] >= 5000:
                raise ValueError('The reference library has reached its size limit.')
            library_id = 'L_' + uuid.uuid4().hex
            c.execute('INSERT INTO library_references VALUES (?,?,?,?,?,?,?)',
                      (library_id, key, _json(record), identity_basis, _json(match) if match else None, stamp, stamp))
        if proposal_link:
            c.execute('INSERT OR IGNORE INTO library_proposal_links VALUES (?,?,?,?)',
                      (library_id, proposal_link['proposal_id'], proposal_link['reference_record_id'], stamp))
        return _entry(c, c.execute('SELECT * FROM library_references WHERE id=?', (library_id,)).fetchone())


def resolve_with_pubmed_preference(identifier, *, transport=None):
    """Resolve an identifier; a DOI is first mapped to its PubMed record when one exists.

    Returns (snapshot, resolved_pmid). The PubMed snapshot keeps the requested DOI
    so library identity covers both identifiers. Mapping failures fall back to the
    DOI record silently; rate limits propagate.
    """
    requested = references.normalize_identifier(identifier)
    if requested['kind'] == 'pmid':
        return references.resolve(identifier, transport=transport), None
    pmid = None
    try:
        pmid = references.pmid_for_doi(requested['value'], transport=transport)
    except references.ReferenceRateLimited:
        raise
    except references.ReferenceError:
        pmid = None
    if pmid:
        snapshot_ = references.resolve(pmid, transport=transport)
        if snapshot_.get('status') == 'resolved':
            found_doi = (snapshot_.get('identifiers') or {}).get('doi')
            if found_doi in (None, requested['value']):
                snapshot_['identifiers']['doi'] = requested['value']
                snapshot_.setdefault('limitations', []).append('PubMed record selected through an exact [doi] field search for the requested DOI.')
                return snapshot_, pmid
    return references.resolve(identifier, transport=transport), None


def _entry(connection, row, *, include_abstract=False):
    snapshot = json.loads(row['snapshot'])
    citations = [dict(r) for r in connection.execute(
        'SELECT * FROM library_citations WHERE reference_id=? ORDER BY project, relative_path, locator', (row['id'],))]
    links = [r['proposal_id'] for r in connection.execute(
        'SELECT proposal_id FROM library_proposal_links WHERE reference_id=?', (row['id'],))]
    entry = {'id': row['id'], 'identity_key': row['identity_key'], 'identity_basis': row['identity_basis'],
             'match': json.loads(row['match']) if row['match'] else None,
             'title': snapshot.get('title'), 'authors': snapshot.get('authors', [])[:3], 'author_count': len(snapshot.get('authors', [])),
             'journal': snapshot.get('journal_abbreviation') or snapshot.get('journal'), 'year': snapshot.get('year'),
             'canonical': snapshot.get('canonical'), 'notices': snapshot.get('publication_notices') or snapshot.get('notices'),
             'cited_in': {'documents': len({c['document_id'] for c in citations}),
                          'projects': sorted({c['project'] for c in citations if c['project']}),
                          'locations': [{k: c[k] for k in ('relative_path', 'project', 'locator', 'title', 'cycle')} for c in citations[:20]]},
             'used_by_proposals': links, 'created_at': row['created_at'], 'updated_at': row['updated_at']}
    check = checks(connection, [row['identity_key']]).get(row['identity_key'])
    entry['flags'] = sorted(set(notice_flags(entry['notices'])) | set((check or {}).get('flags', [])))
    entry['last_checked_at'] = (check or {}).get('checked_at')
    if include_abstract:
        entry['abstract'] = snapshot.get('abstract')
    return entry


def snapshot(store, library_id):
    with store.connect() as c:
        row = c.execute('SELECT * FROM library_references WHERE id=?', (library_id,)).fetchone()
    if row is None:
        raise KeyError('Library reference not found.')
    return json.loads(row['snapshot'])


def count(store):
    with store.connect() as c:
        return c.execute('SELECT COUNT(*) FROM library_references').fetchone()[0]


# ---------------------------------------------------------------- archive scan

def _archive(path):
    uri = path.resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def observed_identifiers(archive_db, *, project=None):
    """Explicit DOIs/PMIDs in the archive with the documents that contain them."""
    found = {}
    with _archive(archive_db) as conn:
        rows = conn.execute('''SELECT i.identifier, i.kind, u.locator, u.section, d.id document_id, d.relative_path,
                                      d.project, d.title, d.role, d.cycle
                               FROM identifiers i JOIN units u ON u.id=i.unit_id JOIN documents d ON d.id=u.document_id
                               WHERE (? = '' OR d.project = ?) AND (d.duplicate_of IS NULL OR d.duplicate_of = '')
                               ORDER BY i.kind, i.identifier, d.relative_path, u.ordinal''', (project or '', project or ''))
        for row in rows:
            identifier = row['identifier']
            if row['kind'] == 'doi':
                identifier = library.clean_doi(identifier)   # archives indexed before the cleaner still carry PDF junk
                if not identifier:
                    continue
            try:
                normalized = references.normalize_identifier(identifier)
            except references.ReferenceInputError:
                continue
            key = normalized['kind'] + ':' + normalized['value']
            item = found.setdefault(key, {'identity_key': key, 'kind': normalized['kind'], 'identifier': normalized['value'],
                                          'occurrences': []})
            item['occurrences'].append({k: row[k] for k in ('document_id', 'relative_path', 'project', 'title', 'role', 'cycle', 'locator', 'section')})
    return sorted(found.values(), key=lambda i: (-len(i['occurrences']), i['identity_key']))


def _looks_like_citation(text):
    return 40 <= len(text) <= 600 and bool(_YEAR.search(text)) and not references._DOI.search(text) and not re.search(r'\bPMID\b', text, re.I)


def reference_entries_without_identifiers(archive_db, *, project=None):
    """Reference-list entries (numbered or one-per-paragraph) that carry no DOI or PMID."""
    entries = {}
    with _archive(archive_db) as conn:
        rows = conn.execute('''SELECT u.text, u.locator, d.id document_id, d.relative_path, d.project, d.title, d.role, d.cycle
                               FROM units u JOIN documents d ON d.id=u.document_id
                               WHERE lower(u.section) IN ('references', 'bibliography', 'literature cited', 'references cited')
                                 AND (? = '' OR d.project = ?) AND (d.duplicate_of IS NULL OR d.duplicate_of = '')
                               ORDER BY d.relative_path, u.ordinal''', (project or '', project or ''))
        for row in rows:
            pieces = _ENTRY_SPLIT.split(row['text'])
            for piece in pieces:
                entry = ' '.join(_LEADING_NUMBER.sub('', piece).split())
                if not _looks_like_citation(entry):
                    continue
                key = 'text:' + hashlib.sha256(' '.join(_WORD.findall(entry.lower())).encode()).hexdigest()[:24]
                item = entries.setdefault(key, {'attempt_key': key, 'entry_text': entry, 'occurrences': []})
                item['occurrences'].append({k: row[k] for k in ('document_id', 'relative_path', 'project', 'title', 'role', 'cycle', 'locator')})
    return sorted(entries.values(), key=lambda i: (-len(i['occurrences']), i['entry_text']))


def match_confidence(entry_text, candidate):
    """Share of informative title words present in the entry, gated by author and year agreement."""
    title_words = [w for w in _WORD.findall((candidate.get('title') or '').lower()) if len(w) > 3 and w not in _STOP]
    entry_words = set(_WORD.findall(entry_text.lower()))
    if not title_words:
        return 0.0
    overlap = sum(1 for w in title_words if w in entry_words) / len(title_words)
    family = (candidate.get('first_author_family') or '').lower()
    if family and family not in entry_text.lower():
        return 0.0
    year = candidate.get('year')
    if year and str(year) not in entry_text and str(year + 1) not in entry_text and str(year - 1) not in entry_text:
        return 0.0
    return round(overlap, 3)


# ---------------------------------------------------------------- harvest

def _record_citations(c, library_id, occurrences, entry_text=None):
    for occurrence in occurrences:
        c.execute('INSERT OR IGNORE INTO library_citations VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (library_id, occurrence['document_id'], occurrence.get('relative_path'), occurrence.get('project'),
                   occurrence.get('title'), occurrence.get('role'), occurrence.get('cycle'), occurrence['locator'],
                   entry_text, _now()))


def _attempt(c, key, status, detail):
    c.execute('INSERT OR REPLACE INTO library_attempts VALUES (?,?,?,?)', (key, status, (detail or '')[:500], _now()))


def pending(store, archive_db, *, project=None, include_unidentified=False, retry_failed=False):
    """Harvest candidates not yet in the library, with already-present ones re-linked to their documents."""
    identifiers = observed_identifiers(archive_db, project=project)
    with store.connect(True) as c:
        present = {row['identity_key']: row['id'] for row in c.execute('SELECT identity_key, id FROM library_references')}
        aliases = {}
        for row in c.execute('SELECT id, snapshot FROM library_references'):
            for key in _keys(json.loads(row['snapshot'])):
                aliases.setdefault(key, row['id'])
        failed = {} if retry_failed else {row['attempt_key']: row['status'] for row in c.execute('SELECT attempt_key, status FROM library_attempts')}
        todo, linked = [], 0
        for item in identifiers:
            library_id = present.get(item['identity_key']) or aliases.get(item['identity_key'])
            if library_id:
                _record_citations(c, library_id, item['occurrences'])
                linked += 1
            elif item['identity_key'] not in failed:
                todo.append(item)
        entries = []
        if include_unidentified:
            for item in reference_entries_without_identifiers(archive_db, project=project):
                if item['attempt_key'] not in failed:
                    entries.append(item)
    return {'identifiers': todo, 'entries': entries, 'already_in_library': linked,
            'skipped_failed': len([k for k in failed if k.startswith(('pmid:', 'doi:')) or include_unidentified and k.startswith('text:')])}


def harvest(store, archive_db, *, project=None, limit=10, include_unidentified=False, retry_failed=False,
            dry_run=False, transport=None):
    """Resolve the next batch of archive references into the library.

    Explicit identifiers are processed before unidentified entries. Each call is
    bounded by limit network resolutions; call again until remaining is zero.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f'limit must be an integer from 1 to {MAX_PAGE}.')
    queue = pending(store, archive_db, project=project, include_unidentified=include_unidentified, retry_failed=retry_failed)
    result = {'project': project or '', 'dry_run': dry_run, 'already_in_library': queue['already_in_library'],
              'pending_identifiers': len(queue['identifiers']), 'pending_unidentified_entries': len(queue['entries']),
              'skipped_after_earlier_failure': queue['skipped_failed'], 'added': [], 'unresolved': [], 'warnings': []}
    if dry_run:
        result['preview'] = ([{'identity_key': i['identity_key'], 'documents': len({o['document_id'] for o in i['occurrences']})}
                              for i in queue['identifiers'][:limit]]
                             + [{'entry_text': e['entry_text'][:200], 'documents': len({o['document_id'] for o in e['occurrences']})}
                                for e in queue['entries'][:max(0, limit - len(queue['identifiers']))]])
        result['remaining'] = len(queue['identifiers']) + len(queue['entries'])
        return result
    budget = limit
    for item in queue['identifiers'][:budget]:
        budget -= 1
        try:
            snapshot_, _pmid = resolve_with_pubmed_preference(item['identifier'], transport=transport)
        except references.ReferenceRateLimited:
            result['warnings'].append('The public reference service asked to slow down; call again later for the remaining items.')
            break
        except references.ReferenceError as error:
            with store.connect(True) as c:
                _attempt(c, item['identity_key'], 'error', str(error))
            result['unresolved'].append({'identity_key': item['identity_key'], 'status': 'error', 'detail': str(error)})
            continue
        if snapshot_.get('status') != 'resolved' or not snapshot_.get('title'):
            with store.connect(True) as c:
                _attempt(c, item['identity_key'], snapshot_.get('status', 'unresolved'), '; '.join(snapshot_.get('limitations', [])[-1:]))
            result['unresolved'].append({'identity_key': item['identity_key'], 'status': snapshot_.get('status', 'unresolved')})
            continue
        entry = remember(store, snapshot_)
        with store.connect(True) as c:
            _record_citations(c, entry['id'], item['occurrences'])
        result['added'].append({'id': entry['id'], 'identity_key': entry['identity_key'], 'observed_identifier': item['identity_key'],
                                'title': entry['title'], 'year': entry['year'], 'identity_basis': 'explicit_identifier',
                                'documents': len({o['document_id'] for o in item['occurrences']})})
    if include_unidentified:
        for item in queue['entries']:
            if budget <= 0:
                break
            budget -= 1
            try:
                candidates = references.lookup_bibliographic(item['entry_text'], transport=transport)
            except references.ReferenceRateLimited:
                result['warnings'].append('The public reference service asked to slow down; call again later for the remaining items.')
                break
            except references.ReferenceError as error:
                with store.connect(True) as c:
                    _attempt(c, item['attempt_key'], 'error', str(error))
                result['unresolved'].append({'entry_text': item['entry_text'][:200], 'status': 'error', 'detail': str(error)})
                continue
            best, confidence = None, 0.0
            for candidate in candidates:
                score = match_confidence(item['entry_text'], candidate)
                if score > confidence:
                    best, confidence = candidate, score
            if best is None or confidence < MATCH_THRESHOLD:
                if best is None and candidates:
                    best = candidates[0]   # reported for manual judgement, with zero confidence
                with store.connect(True) as c:
                    _attempt(c, item['attempt_key'], 'no_confident_match', f'best confidence {confidence}')
                result['unresolved'].append({'entry_text': item['entry_text'][:200], 'status': 'no_confident_match',
                                             'best_candidate': best and {'doi': best['doi'], 'title': best['title'], 'confidence': confidence}})
                continue
            try:
                snapshot_, _pmid = resolve_with_pubmed_preference(best['doi'], transport=transport)
            except references.ReferenceError as error:
                with store.connect(True) as c:
                    _attempt(c, item['attempt_key'], 'error', str(error))
                result['unresolved'].append({'entry_text': item['entry_text'][:200], 'status': 'error', 'detail': str(error)})
                continue
            if snapshot_.get('status') != 'resolved' or not snapshot_.get('title'):
                with store.connect(True) as c:
                    _attempt(c, item['attempt_key'], snapshot_.get('status', 'unresolved'), 'matched DOI did not resolve')
                result['unresolved'].append({'entry_text': item['entry_text'][:200], 'status': snapshot_.get('status', 'unresolved')})
                continue
            match = {'method': 'crossref_query.bibliographic', 'confidence': confidence, 'entry_text': item['entry_text'][:500],
                     'caution': 'Fuzzy match on title words, first author and year; confirm against the original reference list before relying on it.'}
            entry = remember(store, snapshot_, identity_basis='crossref_bibliographic_match', match=match)
            with store.connect(True) as c:
                _record_citations(c, entry['id'], item['occurrences'], item['entry_text'][:500])
                _attempt(c, item['attempt_key'], 'matched', entry['id'])
            result['added'].append({'id': entry['id'], 'identity_key': entry['identity_key'], 'title': entry['title'],
                                    'year': entry['year'], 'identity_basis': 'crossref_bibliographic_match',
                                    'confidence': confidence, 'documents': len({o['document_id'] for o in item['occurrences']})})
    processed_ids = {a.get('observed_identifier', a['identity_key']) for a in result['added']} | {u.get('identity_key') for u in result['unresolved']}
    processed_entries = {a.get('entry_text') for a in result['unresolved'] if a.get('entry_text')} | {
        a['title'] for a in result['added'] if a['identity_basis'] == 'crossref_bibliographic_match'}
    remaining_ids = [i for i in queue['identifiers'] if i['identity_key'] not in processed_ids]
    remaining_entries = [e for e in queue['entries'] if e['entry_text'][:200] not in processed_entries] if include_unidentified else []
    result['remaining'] = max(0, len(remaining_ids) + len(remaining_entries) - len([a for a in result['added'] if a['identity_basis'] != 'explicit_identifier']))
    result['library_total'] = count(store)
    return result


# ---------------------------------------------------------------- query and adoption

def search(store, query='', *, project=None, offset=0, limit=10, include_abstract=False):
    """Rank library entries by query word hits, then by how many archived documents cite them."""
    words = [w for w in _WORD.findall((query or '').lower()) if w not in _STOP]
    with store.connect() as c:
        rows = c.execute('SELECT * FROM library_references').fetchall()
        scored = []
        for row in rows:
            entry = _entry(c, row, include_abstract=True)
            if project and project not in entry['cited_in']['projects'] and project not in entry['used_by_proposals']:
                continue
            # The original reference-list wording of a fuzzy match stays searchable.
            haystack = ' '.join(filter(None, [entry['title'], ' '.join(json.loads(row['snapshot']).get('authors', [])),
                                              entry['journal'], str(entry['year'] or ''), entry.get('abstract') or '',
                                              (entry.get('match') or {}).get('entry_text') or ''])).lower()
            hits = sum(1 for w in words if w in haystack)
            if words and hits == 0:
                continue
            if not include_abstract:
                entry.pop('abstract', None)
            scored.append((hits, entry['cited_in']['documents'], entry['year'] or 0, entry))
    scored.sort(key=lambda s: (-s[0], -s[1], -s[2]))
    items = [s[3] for s in scored]
    page = items[offset:offset + limit]
    return {'items': page, 'total': len(items), 'offset': offset,
            'next_offset': offset + len(page) if offset + len(page) < len(items) else None}


def add_to_proposal(store, proposal_id, expected_revision, library_id):
    """Copy a library record into one proposal's references (no network); reuse an existing R_ record if present."""
    record = snapshot(store, library_id)
    with store.connect() as c:
        store._editable(c, proposal_id, expected_revision)
    proposal = store.get(proposal_id)
    for existing in proposal.get('references', []):
        if _keys(existing) & _keys(record):
            with store.connect(True) as c:
                c.execute('INSERT OR IGNORE INTO library_proposal_links VALUES (?,?,?,?)', (library_id, proposal_id, existing['id'], _now()))
            return {'proposal': proposal, 'reference_id': existing['id'], 'already_present': True}
    proposal = claim_review.add_reference(store, proposal_id, expected_revision, record)
    reference_id = next(r['id'] for r in proposal['references']
                        if _keys(r) & _keys(record))
    with store.connect(True) as c:
        c.execute('INSERT OR IGNORE INTO library_proposal_links VALUES (?,?,?,?)', (library_id, proposal_id, reference_id, _now()))
    return {'proposal': proposal, 'reference_id': reference_id, 'already_present': False}


# ---------------------------------------------------------------- retraction re-check

FLAG_LABELS = {'retracted': 'Retracted', 'expression_of_concern': 'Expression of concern',
               'has_correction': 'Correction issued', 'is_retraction_notice': 'This record is itself a retraction notice',
               'is_correction_notice': 'This record is itself a correction notice',
               'is_expression_of_concern_notice': 'This record is itself an expression-of-concern notice'}
DEFAULT_STALE_DAYS = 30


def notice_flags(notices):
    notices = notices or {}
    return sorted(flag for flag in FLAG_LABELS if notices.get(flag))


def checks(connection, identity_keys=None):
    """Latest re-check per identity key (all keys when none are given)."""
    rows = connection.execute('SELECT * FROM reference_checks').fetchall()
    wanted = set(identity_keys) if identity_keys is not None else None
    result = {}
    for row in rows:
        if wanted is None or row['identity_key'] in wanted:
            result[row['identity_key']] = {'checked_at': row['checked_at'], 'status': row['status'], 'provider': row['provider'],
                                           'resolved_pmid': row['resolved_pmid'],
                                           'flags': json.loads(row['flags']), 'previous_flags': json.loads(row['previous_flags']),
                                           'changed': bool(row['changed']), 'notices': json.loads(row['notices']) if row['notices'] else None}
    return result


def _parse_time(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _is_stale(record, check, stale_days, now):
    reference_time = _parse_time((check or {}).get('checked_at')) or _parse_time(record.get('retrieved_at'))
    if reference_time is None:
        return True
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    return (now - reference_time).days >= stale_days


def status_for(records, connection, *, stale_days=DEFAULT_STALE_DAYS, now=None):
    """Flag and staleness summary for a list of reference snapshots (no network)."""
    now = now or datetime.now(timezone.utc)
    latest = checks(connection, [identity_key(r) for r in records if identity_key(r)])
    flagged, stale, items = [], [], {}
    for record in records:
        key = identity_key(record)
        if not key:
            continue
        check = latest.get(key)
        flags = sorted(set(notice_flags(record.get('publication_notices') or record.get('notices'))) | set((check or {}).get('flags', [])))
        item = {'identity_key': key, 'title': record.get('title'), 'flags': flags,
                'labels': [FLAG_LABELS[f] for f in flags], 'last_checked_at': (check or {}).get('checked_at'),
                'stale': _is_stale(record, check, stale_days, now)}
        items[key] = item
        if flags:
            flagged.append(item)
        if item['stale']:
            stale.append(key)
    return {'items': items, 'flagged': flagged, 'stale_identity_keys': stale, 'stale_days': stale_days}


def recheck(store, *, proposal_id=None, limit=20, stale_days=DEFAULT_STALE_DAYS, force=False, transport=None, now=None):
    """Re-resolve references against PubMed/Crossref and record any new retraction, concern or correction.

    proposal_id limits the check to that proposal's references; otherwise the
    whole shared library is checked. Only the next limit stale records are
    fetched per call (force treats every record as stale). Stored snapshots stay
    immutable; the outcome lives in reference_checks and is shown at export.
    """
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise ValueError(f'limit must be an integer from 1 to {MAX_PAGE}.')
    if type(stale_days) is not int or not 0 <= stale_days <= 3650:
        raise ValueError('stale_days must be an integer from 0 to 3650.')
    now = now or datetime.now(timezone.utc)
    if proposal_id:
        records = store.get(proposal_id)['references']
    else:
        with store.connect() as c:
            records = [json.loads(r['snapshot']) for r in c.execute('SELECT snapshot FROM library_references ORDER BY created_at')]
    unique = {}
    for record in records:
        key = identity_key(record)
        if key and key not in unique:
            unique[key] = record
    with store.connect() as c:
        summary = status_for(list(unique.values()), c, stale_days=0 if force else stale_days, now=now)
    due = [unique[k] for k in summary['stale_identity_keys']]
    result = {'scope': proposal_id or 'library', 'references': len(unique), 'due': len(due), 'checked': [],
              'newly_flagged': [], 'warnings': [], 'stale_days': stale_days}
    for record in due[:limit]:
        canonical = record.get('canonical') or record.get('identifiers') or {}
        identifier = canonical.get('pmid') or canonical.get('doi')
        key = identity_key(record)
        # "Previous" is everything already known: the snapshot plus the last recorded check.
        previous = summary['items'][key]['flags']
        try:
            fresh, resolved_pmid = resolve_with_pubmed_preference(str(identifier), transport=transport)
        except references.ReferenceRateLimited:
            result['warnings'].append('The public reference service asked to slow down; call again later for the remaining items.')
            break
        except references.ReferenceError as error:
            result['checked'].append({'identity_key': key, 'title': record.get('title'), 'status': 'error', 'detail': str(error)})
            continue
        if fresh.get('status') != 'resolved':
            with store.connect(True) as c:
                c.execute('INSERT OR REPLACE INTO reference_checks VALUES (?,?,?,?,?,?,?,?,?)',
                          (key, now.isoformat(), fresh.get('status', 'unresolved'), fresh.get('provider'), _json(previous), None, _json(previous), 0, None))
            result['checked'].append({'identity_key': key, 'title': record.get('title'), 'status': fresh.get('status', 'unresolved'),
                                      'detail': 'The identifier no longer resolves to a single record; confirm the reference manually.'})
            continue
        flags = notice_flags(fresh.get('publication_notices'))
        changed = flags != previous
        with store.connect(True) as c:
            c.execute('INSERT OR REPLACE INTO reference_checks VALUES (?,?,?,?,?,?,?,?,?)',
                      (key, now.isoformat(), 'checked', fresh.get('provider'), _json(flags), _json(fresh.get('publication_notices')),
                       _json(previous), int(changed), resolved_pmid))
        entry = {'identity_key': key, 'title': record.get('title'), 'status': 'checked', 'provider': fresh.get('provider'),
                 'resolved_pmid': resolved_pmid, 'flags': flags, 'labels': [FLAG_LABELS[f] for f in flags],
                 'changed': changed, 'previous_flags': previous}
        result['checked'].append(entry)
        if changed and set(flags) - set(previous):
            result['newly_flagged'].append(entry)
    result['remaining'] = max(0, len(due) - len(result['checked']))
    with store.connect() as c:
        after = status_for(list(unique.values()), c, stale_days=stale_days, now=now)
    result['flagged'] = after['flagged']
    result['limitations'] = ['Flags come from the provider record (PubMed CommentsCorrections and publication types, or Crossref update/relation fields); '
                             'an absent flag does not prove the paper is unretracted.',
                             'PubMed is preferred because its notice links are directional and more complete; a DOI-only reference is first mapped to its PubMed record through an exact [doi] search, and falls back to Crossref when PubMed has none.']
    return result
