"""Bounded refresh of public NIH project descriptions, separate from drafts.

RePORTER documents ``date_added`` as an addition date, not a modification
cursor (https://api.reporter.nih.gov/, consulted 2026-10-31). Consequently
incremental discovery is complemented by full topic reconciliation every
28 days and rotating by-ID checks. Comparison records must be downloaded;
only new/changed payloads are imported. No private archive is read here.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from . import nih

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _now():
    return datetime.now(timezone.utc)


def _int(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{name} must be an integer from {minimum} to {maximum}.')
    return value


def _settings(settings):
    if not isinstance(settings, dict):
        raise ValueError('NIH refresh settings must be an object.')
    queries = settings.get('queries', [])
    if not isinstance(queries, list) or not 1 <= len(queries) <= 30 or any(
            not isinstance(q, str) or not q.strip() or len(q) > 300 for q in queries):
        raise ValueError('NIH queries must contain 1..30 short public research topics.')
    if 'rolling_years' in settings:
        if 'fiscal_years' in settings:
            raise ValueError('Choose NIH rolling_years or fiscal_years, not both.')
        width = _int(settings['rolling_years'], 'rolling_years', 1, 20)
        today = _now()
        fiscal_year = today.year + (today.month >= 10)
        years = list(range(fiscal_year - width + 1, fiscal_year + 1))
    else:
        years = settings.get('fiscal_years', [])
    if not isinstance(years, list) or not years or any(
            type(y) is not int or not 1900 <= y <= _now().year + 1 for y in years):
        raise ValueError('NIH fiscal_years must be an explicit list of valid years.')
    fields = settings.get('search_fields', 'projecttitle,abstracttext')
    if fields not in {'projecttitle,abstracttext,terms', 'projecttitle,abstracttext',
                       'projecttitle', 'abstracttext'}:
        raise ValueError('Use documented NIH title/abstract/term search fields.')
    return {
        'queries': list(dict.fromkeys(q.strip() for q in queries)),
        'fiscal_years': sorted(set(years)), 'search_fields': fields,
        'max_records': _int(settings.get('max_records', 1500), 'max_records', 1, nih.API_WINDOW),
        'page_size': _int(settings.get('page_size', 100), 'page_size', 1, 500),
        'reconciliation_days': _int(settings.get('reconciliation_days', 28), 'reconciliation_days', 1, 90),
        'max_reconcile_records': _int(settings.get('max_reconcile_records', 1000), 'max_reconcile_records', 0, 15000),
        'max_requests': _int(settings.get('max_requests', 150), 'max_requests', 1, 1000),
        'overlap_days': _int(settings.get('overlap_days', 7), 'overlap_days', 1, 30),
    }


def _request_json(payload):
    """Respect NIH's <=1 request/sec guidance; bound retries and response size."""
    for attempt in range(3):
        time.sleep(1.05)
        request = urllib.request.Request(nih.API_URL, data=nih._json(payload).encode(),
            headers={'Content-Type': 'application/json', 'Accept': 'application/json',
                     'User-Agent': 'grantlib-refresh/1.0 (bounded public NIH research)'}, method='POST')
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError('NIH response exceeded the size limit.')
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            retry = error.headers.get('Retry-After', '')
            code = error.code
            error.close()
            if code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise
            try:
                delay = float(retry)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(retry).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 2 ** (attempt + 1)
            # A long server-requested pause ends this attempt rather than
            # disregarding Retry-After or keeping the weekly worker stuck.
            if delay > 60:
                raise RuntimeError('NIH requested a retry after more than 60 seconds.') from error
            time.sleep(max(1, delay))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError('NIH retry budget exhausted.')


def _collect(stage, criteria, cap, page_size, request):
    """Stage a bounded query without mutating the usable NIH index."""
    run = {'run_id': uuid.uuid4().hex, 'criteria': criteria, 'received': 0,
           'total': None, 'search_id': None, 'status': 'partial', 'warnings': [], 'ids': []}
    seen = set()
    while run['received'] < cap:
        limit = min(page_size, cap - run['received'])
        payload = {'offset': run['received'], 'limit': limit,
                   'sort_field': 'appl_id', 'sort_order': 'asc'}
        payload.update({'search_id': run['search_id']} if run['search_id'] else {'criteria': criteria})
        response = request(payload)
        if not isinstance(response, dict) or not isinstance(response.get('results'), list):
            raise ValueError('NIH response lacks a results list.')
        rows = response['results']
        meta = response.get('meta') or {}
        total = meta.get('total')
        if type(total) is not int or total < 0:
            raise ValueError('NIH response lacks a valid total count.')
        if run['total'] is not None and total != run['total']:
            run['warnings'].append('reported_total_changed_during_query')
        run['total'] = total
        run['search_id'] = meta.get('search_id') or run['search_id']
        if len(rows) > limit or len(rows) > max(0, total - run['received']):
            raise ValueError('NIH page count exceeds the requested limit or reported total.')
        # Validate the whole page before staging any of it. A by-ID response
        # must not import an unrelated record if the upstream API misbehaves.
        for item in rows:
            if not isinstance(item, dict) or not re.fullmatch(r'\d+', str(item.get('appl_id', ''))):
                raise ValueError('NIH record lacks a numeric application ID.')
            if 'appl_ids' in criteria and int(item['appl_id']) not in criteria['appl_ids']:
                raise ValueError('NIH returned IDs outside the by-ID request.')
        for item in rows:
            app_id = str(item['appl_id'])
            if app_id in seen:
                run['warnings'].append('duplicate_application_ids_in_query')
            seen.add(app_id)
            stage.execute('INSERT OR REPLACE INTO records VALUES(?,?)', (app_id, nih._json(item)))
        run['received'] += len(rows)
        if run['received'] >= total:
            if not run['warnings']:
                run['status'] = 'complete'
            break
        if not rows:
            run['warnings'].append('empty_page_before_confirmed_end')
            break
    if run['total'] is not None and run['total'] > run['received']:
        run['warnings'].append('requested_cap_reached' if run['received'] >= cap else 'incomplete_query')
    run['ids'] = sorted(seen)
    return run


def _checkpoint_date(checkpoint, key):
    value = checkpoint.get(key)
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except (ValueError, TypeError):
        return None


def _known_ids(conn, years, after, maximum):
    if not maximum:
        return []
    placeholders = ','.join('?' for _ in years)
    rows = conn.execute(f'SELECT application_id FROM nih_records WHERE fiscal_year IN ({placeholders}) '
                        'AND CAST(application_id AS INTEGER)>? ORDER BY CAST(application_id AS INTEGER) LIMIT ?',
                        [*years, after, maximum]).fetchall()
    # Start the next rotation only on the next run, so a checkpoint is a simple
    # monotonically increasing ID within one sweep rather than an offset.
    if not rows and after:
        return _known_ids(conn, years, 0, maximum)
    return [r[0] for r in rows]


def _apply(conn, stage, runs, missing, timestamp, cap):
    """One short transaction publishes records and observation provenance."""
    counts = {'new': 0, 'changed': 0, 'unchanged': 0, 'missing_by_id': len(missing)}
    conn.execute('''CREATE TABLE IF NOT EXISTS nih_refresh_observations (
      application_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL,
      reporter_presence TEXT NOT NULL, payload_sha256 TEXT)''')
    with conn:
        for app_id, raw in stage.execute('SELECT application_id,raw_json FROM records'):
            old = conn.execute('SELECT raw_json FROM nih_records WHERE application_id=?', (app_id,)).fetchone()
            digest = nih._hash(raw)
            if old and nih._json(json.loads(old[0])) == raw:
                counts['unchanged'] += 1
            else:
                counts['changed' if old else 'new'] += 1
                record = json.loads(raw)
                source_id = 'refresh:' + uuid.uuid4().hex
                nih._source(conn, source_id=source_id, source_type='api_refresh_record', origin=nih.API_URL,
                            metadata={'payload_sha256': digest, 'observed_at': timestamp}, raw=record,
                            sha256=digest, row_count=1)
                nih.import_api_record(conn, record, source_id)
                # The legacy importer coalesces blanks to protect sparse
                # ExPORTER imports. Here explicit API values, including clears,
                # should supersede stale displayed metadata.
                text_fields = {'project_title': 'title', 'core_project_num': 'core_project_number',
                    'project_num': 'project_number', 'subproject_id': 'subproject_id',
                    'phr_text': 'public_health_relevance', 'activity_code': 'activity',
                    'project_start_date': 'project_start', 'project_end_date': 'project_end',
                    'budget_start': 'budget_start', 'budget_end': 'budget_end',
                    'project_detail_url': 'reporter_url', 'principal_investigators': 'principal_investigators'}
                updates = {column: nih._text(record[key]) for key, column in text_fields.items() if key in record}
                for key, column in (('award_amount', 'award_amount'), ('total_cost_sub_project', 'total_cost_subproject')):
                    if key in record:
                        updates[column] = nih._number(record[key])
                if updates:
                    conn.execute('UPDATE nih_records SET ' + ','.join(key + '=?' for key in updates) +
                                 ' WHERE application_id=?', [*updates.values(), app_id])
                # A full API response explicitly blanking an abstract is a
                # source change. The older raw version remains recoverable.
                if 'abstract_text' in record and not record['abstract_text']:
                    conn.execute("UPDATE nih_records SET abstract='',abstract_source_id=? WHERE application_id=?",
                                 (source_id, app_id))
            conn.execute('INSERT OR REPLACE INTO nih_refresh_observations VALUES(?,?,?,?)',
                         (app_id, timestamp, 'present', digest))
        for app_id in missing:
            conn.execute('INSERT OR REPLACE INTO nih_refresh_observations VALUES(?,?,?,NULL)',
                         (app_id, timestamp, 'not_returned_by_id'))
        for run in runs:
            # Reuse the existing collection/provenance view for refreshed
            # topic queries; by-ID requests are observation checks, not topics.
            if 'advanced_text_search' not in run['criteria']:
                continue
            conn.execute('''INSERT INTO nih_api_runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (run['run_id'], nih._hash(nih._json(run['criteria'])), nih._json(run['criteria']),
                 run['received'], run['received'], run['total'], run['search_id'], cap,
                 run['status'], timestamp, timestamp, nih._json(run['warnings']), None))
            conn.executemany('INSERT INTO nih_api_run_records VALUES(?,?)',
                             [(run['run_id'], item) for item in run['ids']])
    return counts


def refresh(data_dir, settings, checkpoint=None):
    """Refresh public topics, returning a successful cursor only on completion.

    ``max_records`` is per topic (<=15000); ``max_requests`` bounds the entire
    run, with <=3 transport attempts each. Partial results may add usable
    records atomically, but never advance the checkpoint. Source snapshots and
    stored proposals are not rewritten. A changed query/year scope forces a
    fresh baseline. ``rolling_years`` optionally selects N federal fiscal years
    through the current FY (which changes on October 1), instead of explicit
    ``fiscal_years``. Only this public NIH database is read.
    """
    settings = _settings(settings)
    prior = dict(checkpoint or {})
    now = _now()
    today = now.date()
    signature = nih._hash(nih._json({k: settings[k] for k in ('queries', 'fiscal_years', 'search_fields')}))
    valid_prior = prior if prior.get('scope_sha256') == signature else {}
    discovery = _checkpoint_date(valid_prior, 'discovery_through')
    reconciled = _checkpoint_date(valid_prior, 'reconciled_on')
    full = not discovery or not reconciled or (today - reconciled).days >= settings['reconciliation_days']
    if discovery and discovery > today:
        raise ValueError('NIH checkpoint is in the future; refusing to skip discovery.')
    data_dir = Path(data_dir).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)
    requests = 0

    def request(payload):
        nonlocal requests
        if requests >= settings['max_requests']:
            raise RuntimeError('NIH refresh request budget reached.')
        requests += 1
        return _request_json(payload)

    runs, missing, warnings = [], set(), []
    failure = None
    id_cursor = int(valid_prior.get('recheck_after_id', 0))
    with closing(nih.initialize(data_dir / 'nih.sqlite3')) as conn:
        known = _known_ids(conn, settings['fiscal_years'], id_cursor, settings['max_reconcile_records'])
        with tempfile.TemporaryDirectory(prefix='nih-refresh-', dir=data_dir) as temporary:
            with closing(sqlite3.connect(Path(temporary) / 'stage.sqlite3')) as stage:
                stage.execute('CREATE TABLE records(application_id TEXT PRIMARY KEY,raw_json TEXT NOT NULL)')
                try:
                    for query in settings['queries']:
                        criteria = {'fiscal_years': settings['fiscal_years'], 'advanced_text_search': {
                            'operator': 'and', 'search_field': settings['search_fields'], 'search_text': query}}
                        if not full:
                            criteria['date_added'] = {'from_date': (discovery - timedelta(days=settings['overlap_days'])).isoformat(),
                                                      'to_date': today.isoformat()}
                        run = _collect(stage, criteria, settings['max_records'], settings['page_size'], request)
                        runs.append(run)
                    for start in range(0, len(known), 100):
                        batch = known[start:start + 100]
                        run = _collect(stage, {'appl_ids': [int(v) for v in batch]}, len(batch), 100, request)
                        runs.append(run)
                        if run['status'] == 'complete':
                            returned = set(run['ids'])
                            if not returned <= set(batch):
                                raise ValueError('NIH returned IDs outside the by-ID request.')
                            missing.update(set(batch) - returned)
                            id_cursor = int(batch[-1])
                except (OSError, ValueError, RuntimeError) as error:
                    failure = error
                    warnings.append(f'request_failed:{type(error).__name__}')
                observed_count = stage.execute('SELECT count(*) FROM records').fetchone()[0]
                if failure and not runs and not observed_count:
                    raise failure
                # If a topic response and by-ID response disagree, retain the
                # record but flag the later by-ID absence for manual inspection.
                counts = _apply(conn, stage, runs, missing, now.isoformat(), settings['max_records'])
    complete = failure is None and all(run['status'] == 'complete' for run in runs)
    for run in runs:
        warnings.extend(run['warnings'])
    if missing:
        warnings.append('some_known_application_ids_not_returned_preserved_for_review')
    next_checkpoint = prior
    if complete:
        next_checkpoint = {'scope_sha256': signature, 'discovery_through': today.isoformat(),
                           'reconciled_on': today.isoformat() if full else reconciled.isoformat(),
                           'recheck_after_id': id_cursor}
    return {
        'status': 'complete' if complete else 'partial', 'counts': counts | {
            'observed': observed_count, 'requests': requests, 'queries': len(settings['queries']),
            'known_ids_requested': len(known)},
        'checkpoint': next_checkpoint, 'warnings': sorted(set(warnings)),
        'mode': 'full_topic_reconciliation' if full else 'incremental_additions',
        'coverage': 'configured_public_topics_and_years_only',
        'change_detection': 'date_added finds additions only; full-topic and rotating ID reconciliation detects corrections',
        'queries': [{'criteria': r['criteria'], 'received': r['received'], 'total': r['total'],
                     'status': r['status']} for r in runs if 'advanced_text_search' in r['criteria']],
        'evidence_label': nih.EVIDENCE_LABEL,
    }


def annotate_results(data_dir, results):
    """Attach observed freshness without changing or concealing stored records."""
    path = Path(data_dir) / 'nih.sqlite3'
    if not path.is_file():
        return results
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='nih_refresh_observations'").fetchone():
            return results
        enriched = []
        for item in results:
            row = conn.execute('SELECT observed_at,reporter_presence FROM nih_refresh_observations WHERE application_id=?',
                               (item.get('application_id'),)).fetchone()
            enriched.append(dict(item) | ({'observed_at': row[0], 'reporter_presence': row[1]} if row else {
                'observed_at': None, 'reporter_presence': 'not_rechecked'}))
        return enriched
