"""Incremental public PubMed discovery, separate from retained proposal evidence.

Official API/field specifications consulted 2026-10-31:
https://www.ncbi.nlm.nih.gov/books/NBK25499/
https://pubmed.ncbi.nlm.nih.gov/help/#modification-date-lr
https://www.nlm.nih.gov/bsd/policy/errata.html

PubMed's documented [lr] (completed-citation revision) and [crdt] (creation)
fields are used together, with an overlap. A bounded oldest-first recheck of
known PMIDs also detects changes that no longer match the public topic query.
This is a targeted discovery corpus, not an exhaustive literature review or
NLM's full baseline/update-file mirror. No personal or Studio database is read.
"""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urlencode
import uuid
import xml.etree.ElementTree as ET

from . import references

API_BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
MAX_BYTES = 4 * 1024 * 1024
BATCH_SIZE = 50


class PublicationRefreshError(RuntimeError):
    """Refresh failure with no raw public response or private context in errors."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _integer(value, label, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{label} must be an integer from {low} to {high}.')
    return value


def _settings(settings):
    queries = settings.get('queries')
    if (not isinstance(queries, list) or not 1 <= len(queries) <= 10
            or any(not isinstance(q, str) or not q.strip() or len(q) > 1000 for q in queries)):
        raise ValueError('PubMed queries must contain 1–10 explicit public-topic strings, at most 1,000 characters each.')
    return (list(dict.fromkeys(q.strip() for q in queries)),
            _integer(settings.get('max_records', 500), 'max_records', 1, 5000),
            _integer(settings.get('initial_days', 365), 'initial_days', 1, 36525),
            _integer(settings.get('overlap_days', 7), 'overlap_days', 1, 90),
            _integer(settings.get('reconcile_records', 100), 'reconcile_records', 0, 1000))


def initialize(data_dir):
    path = Path(data_dir) / 'publications.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS pubmed_records (
        pmid TEXT PRIMARY KEY, record_sha256 TEXT NOT NULL, record_json TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, last_checked_at TEXT NOT NULL,
        retrieved_at TEXT NOT NULL, unavailable_at TEXT
      );
      CREATE TABLE IF NOT EXISTS pubmed_versions (
        pmid TEXT NOT NULL, record_sha256 TEXT NOT NULL, record_json TEXT NOT NULL,
        raw_xml TEXT NOT NULL, first_seen_at TEXT NOT NULL,
        PRIMARY KEY(pmid, record_sha256)
      );
      CREATE VIRTUAL TABLE IF NOT EXISTS pubmed_fts USING fts5(
        pmid UNINDEXED, title, abstract, terms, tokenize='unicode61'
      );
      CREATE TABLE IF NOT EXISTS pubmed_refresh_runs (
        run_id TEXT PRIMARY KEY, query_key TEXT NOT NULL, query TEXT NOT NULL,
        window_start TEXT NOT NULL, window_end TEXT NOT NULL,
        total_count INTEGER, received_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
        query_url TEXT, error TEXT
      );
    ''')
    conn.commit()
    return conn


def _request(url):
    """Fixed-host requests, one/sec shared throttle, at most one short retry."""
    for attempt in range(2):
        try:
            response = references._request_official(
                url, timeout=15, max_bytes=MAX_BYTES,
                hosts={'eutils.ncbi.nlm.nih.gov'}, user_agent='GrantLibrary-PublicRefresh/1.0')
        except references.ReferenceRateLimited as error:
            if attempt or error.retry_after > 3:
                raise PublicationRefreshError('PubMed rate limit prevented this refresh.') from None
            time.sleep(error.retry_after)
            continue
        except (references.ReferenceError, OSError):
            if attempt:
                raise PublicationRefreshError('PubMed could not be reached within the request limit.') from None
            time.sleep(1)
            continue
        if response.status == 200:
            return response.body
        delay = references._retry_after({str(k).lower(): str(v) for k, v in response.headers.items()})
        if response.status in (429, 500, 502, 503, 504) and not attempt and delay <= 3:
            time.sleep(delay)
            continue
        raise PublicationRefreshError(f'PubMed returned HTTP {response.status}; no response body was accepted.')
    raise PublicationRefreshError('PubMed request did not complete.')


def _date_term(query, start, end):
    interval = f'{start.strftime("%Y/%m/%d")}:{end.strftime("%Y/%m/%d")}'
    return f'({query}) AND ({interval}[lr] OR {interval}[crdt])'


def _discover(query, start, end, cap):
    url = API_BASE + 'esearch.fcgi?' + urlencode({
        'db': 'pubmed', 'retmode': 'json', 'term': _date_term(query, start, end),
        'retmax': cap, 'retstart': 0, 'sort': 'pub_date', 'tool': 'grantlibrary_public_refresh'})
    try:
        payload = json.loads(_request(url))
        result = payload['esearchresult']
        count = int(result['count'])
        ids = result['idlist']
        if (count < 0 or not isinstance(ids, list) or len(ids) > cap
                or any(not isinstance(i, str) or not re.fullmatch(r'[1-9][0-9]{0,9}', i) for i in ids)
                or len(ids) != len(set(ids)) or len(ids) != min(count, cap)
                or result.get('errorlist') or payload.get('error') or result.get('error')):
            raise ValueError('Invalid search result.')
    except (KeyError, TypeError, ValueError, RecursionError):
        raise PublicationRefreshError('PubMed returned an invalid or incomplete search result.') from None
    return ids, count, url


def _fetch(ids):
    """Return exact per-article XML plus parsed metadata; reject unsolicited IDs."""
    url = API_BASE + 'efetch.fcgi?' + urlencode({
        'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml', 'tool': 'grantlibrary_public_refresh'})
    raw = _request(url)
    upper = raw.upper()
    if b'<!ENTITY' in upper or re.search(br'<!DOCTYPE[^>]*\[', upper):
        raise PublicationRefreshError('PubMed XML contains unsupported entity declarations.')
    try:
        root = ET.fromstring(raw)
        stack, nodes = [(root, 0)], 0
        while stack:
            node, depth = stack.pop()
            nodes += 1
            if nodes > 250000 or depth > 100:
                raise ValueError('XML structural limit.')
            stack.extend((child, depth + 1) for child in node)
        if root.tag != 'PubmedArticleSet':
            raise ValueError('Unexpected document.')
        accepted = {}
        for item in root:
            if item.tag not in ('PubmedArticle', 'PubmedBookArticle'):
                raise ValueError('Unexpected element.')
            primary = item.find('MedlineCitation' if item.tag == 'PubmedArticle' else 'BookDocument')
            pmid = references._text(primary.find('PMID')) if primary is not None else None
            if pmid not in ids or pmid in accepted:
                raise ValueError('Unexpected PMID.')
            # Serialization retains the article subtree, not the surrounding response/DTD.
            xml = ET.tostring(item, encoding='unicode')
            wrapped = ('<PubmedArticleSet>' + xml + '</PubmedArticleSet>').encode('utf-8')
            snapshot = references._snapshot({'kind': 'pmid', 'value': pmid}, 'pubmed', url, wrapped)
            references._pubmed(wrapped, snapshot)
            if snapshot['status'] != 'resolved':
                raise ValueError('Unresolved PMID.')
            snapshot['mesh_terms'] = [references._text(n) for n in primary.findall('MeshHeadingList/MeshHeading/DescriptorName')]
            snapshot['keywords'] = [references._text(n) for n in primary.findall('KeywordList/Keyword')]
            snapshot['date_revised'] = '/'.join(references._text(primary.find('DateRevised/' + part)) or ''
                                                 for part in ('Year', 'Month', 'Day')).strip('/') or None
            snapshot['record_xml_sha256'] = _sha(xml)
            snapshot['response_sha256'] = hashlib.sha256(raw).hexdigest()
            snapshot['record_xml_format'] = 'ElementTree serialization of the returned article subtree; batch response SHA-256 retained separately.'
            if 'Retraction Notice' in snapshot.get('publication_types', []):
                snapshot['publication_notices']['is_retraction_notice'] = True
            snapshot['evidence_label'] = 'publication_metadata_and_abstract'
            snapshot['retained_as_proposal_evidence'] = False
            accepted[pmid] = (snapshot, xml)
        return accepted
    except (ET.ParseError, ValueError, TypeError, RecursionError, references.ReferenceError):
        raise PublicationRefreshError('PubMed returned invalid or mismatched article XML.') from None


def _store(conn, snapshot, xml, checked_at):
    pmid, digest = snapshot['identifiers']['pmid'], snapshot['record_xml_sha256']
    previous = conn.execute('SELECT record_sha256 FROM pubmed_records WHERE pmid=?', (pmid,)).fetchone()
    payload = _json(snapshot)
    conn.execute('INSERT OR IGNORE INTO pubmed_versions VALUES(?,?,?,?,?)',
                 (pmid, digest, payload, xml, checked_at))
    conn.execute('''INSERT INTO pubmed_records VALUES(?,?,?,?,?,?,NULL)
        ON CONFLICT(pmid) DO UPDATE SET record_sha256=excluded.record_sha256,
        record_json=excluded.record_json, last_checked_at=excluded.last_checked_at,
        retrieved_at=excluded.retrieved_at, unavailable_at=NULL''',
                 (pmid, digest, payload, checked_at, checked_at, checked_at))
    if previous is None or previous['record_sha256'] != digest:
        conn.execute('DELETE FROM pubmed_fts WHERE pmid=?', (pmid,))
        conn.execute('INSERT INTO pubmed_fts VALUES(?,?,?,?)', (pmid, snapshot.get('title') or '',
                     snapshot.get('abstract') or '', ' '.join(filter(None,
                         snapshot['mesh_terms'] + snapshot['keywords'] + snapshot['authors'] +
                         [pmid, snapshot['identifiers'].get('doi')]))))
    return 'new' if previous is None else ('changed' if previous['record_sha256'] != digest else 'unchanged')


def refresh(data_dir, settings, checkpoint=None):
    """Refresh explicit public queries; only complete query windows move forward.

    max_records is a per-query discovery cap (1..5000), not an assertion of
    complete coverage. A capped query retains its old watermark; narrow its
    query/window or raise the cap before calling the corpus current. Batches
    that fail or omit discovered IDs do not publish that query's record changes.
    Reconciliation checks bounded oldest known records independently of queries.
    """
    queries, cap, initial_days, overlap, reconcile = _settings(settings)
    prior = checkpoint if isinstance(checkpoint, dict) else {}
    prior_queries = prior.get('queries', {}) if isinstance(prior.get('queries', {}), dict) else {}
    next_queries = dict(prior_queries)
    today, started = datetime.now(timezone.utc).date(), _now()
    counts = {'new': 0, 'changed': 0, 'unchanged': 0, 'discovered': 0,
              'queries_complete': 0, 'queries_partial': 0, 'queries_failed': 0,
              'reconciled': 0, 'unavailable': 0}
    warnings, coverage, completed_requests = [], [], 0
    refreshed_ids = set()
    with closing(initialize(data_dir)) as conn:
        # Take this inventory before discovery, so a new corpus does not immediately
        # duplicate every fetch and old records keep getting their turn.
        recheck = [row['pmid'] for row in conn.execute(
            'SELECT pmid FROM pubmed_records ORDER BY last_checked_at, pmid LIMIT ?', (reconcile,))]
        for query in queries:
            key = _sha(query)
            old = prior_queries.get(key, {})
            if not isinstance(old, dict):
                old = {}
            try:
                through = date.fromisoformat(old['through']) if old.get('query') == query else None
            except (ValueError, KeyError, TypeError):
                through = None
            if through and through > today:
                raise ValueError('PubMed checkpoint is in the future.')
            desired_start = today - timedelta(days=initial_days)
            try:
                initial_from = date.fromisoformat(old['initial_from'])
            except (ValueError, KeyError, TypeError):
                initial_from = None
            # A larger requested historical scope must not reuse a narrower
            # watermark and silently omit the newly requested older records.
            expanded = through is not None and (initial_from is None or desired_start < initial_from)
            start = (through - timedelta(days=overlap)
                     if through and not expanded else desired_start)
            run = uuid.uuid4().hex
            conn.execute('INSERT INTO pubmed_refresh_runs(run_id,query_key,query,window_start,window_end,status,started_at) VALUES(?,?,?,?,?,?,?)',
                         (run, key, query, start.isoformat(), today.isoformat(), 'running', started))
            conn.commit()
            record = {'query': query, 'window_start': start.isoformat(), 'window_end': today.isoformat(),
                      'date_fields': ['lr', 'crdt'], 'run_id': run, 'cap': cap}
            try:
                ids, total, url = _discover(query, start, today, cap)
                staged = {}
                for offset in range(0, len(ids), BATCH_SIZE):
                    batch = ids[offset:offset + BATCH_SIZE]
                    records = _fetch(batch)
                    if set(records) != set(batch):
                        raise PublicationRefreshError('PubMed omitted a discovered PMID; this query will be retried.')
                    staged.update(records)
                checked = _now()
                delta = {'new': 0, 'changed': 0, 'unchanged': 0}
                with conn:
                    for snapshot, xml in staged.values():
                        delta[_store(conn, snapshot, xml, checked)] += 1
                    state = 'complete' if total <= cap else 'partial'
                    conn.execute('UPDATE pubmed_refresh_runs SET total_count=?,received_count=?,status=?,finished_at=?,query_url=? WHERE run_id=?',
                                 (total, len(ids), state, checked, url, run))
                completed_requests += 1
                refreshed_ids.update(staged)
                for name, amount in delta.items():
                    counts[name] += amount
                counts['discovered'] += len(ids)
                counts['queries_' + state] += 1
                record.update(status=state, total=total, fetched=len(ids), query_url=url)
                if state == 'complete':
                    next_queries[key] = {'query': query, 'through': today.isoformat(),
                                         'initial_from': min(initial_from, start).isoformat() if initial_from else start.isoformat(),
                                         'initial_days': initial_days,
                                         'last_complete_at': checked}
                else:
                    warnings.append(f'PubMed query {key[:12]} matched {total} records; cap {cap} leaves incomplete coverage. Its watermark did not advance.')
            except (PublicationRefreshError, references.ReferenceError) as error:
                counts['queries_failed'] += 1
                with conn:
                    conn.execute('UPDATE pubmed_refresh_runs SET status=?,finished_at=?,error=? WHERE run_id=?',
                                 ('failed', _now(), str(error), run))
                record.update(status='failed', error=str(error))
                warnings.append(f'PubMed query {key[:12]} failed; its prior records and watermark are retained.')
            coverage.append(record)
        recheck = [pmid for pmid in recheck if pmid not in refreshed_ids]
        for offset in range(0, len(recheck), BATCH_SIZE):
            batch = recheck[offset:offset + BATCH_SIZE]
            try:
                found = _fetch(batch)
                checked = _now()
                with conn:
                    for pmid in batch:
                        if pmid in found:
                            counts[_store(conn, *found[pmid], checked)] += 1
                        else:
                            # Missing does not establish deletion/retraction; keep its
                            # old snapshot but explicitly flag unavailability in search.
                            conn.execute('UPDATE pubmed_records SET last_checked_at=?, unavailable_at=? WHERE pmid=?',
                                         (checked, checked, pmid))
                            counts['unavailable'] += 1
                    counts['reconciled'] += len(batch)
                completed_requests += 1
            except (PublicationRefreshError, references.ReferenceError):
                warnings.append('The bounded known-PMID reconciliation did not complete; prior records were retained.')
                break
    if not completed_requests:
        raise PublicationRefreshError('No PubMed query or reconciliation completed; prior publication records were retained.')
    partial = bool(counts['queries_partial'] or counts['queries_failed'] or counts['reconciled'] != len(recheck))
    return {'status': 'partial' if partial else 'complete', 'counts': counts,
            'checkpoint': {'version': 1, 'queries': next_queries}, 'warnings': warnings,
            'coverage': coverage, 'checked_at': _now(),
            'scope': 'Explicit public topics in creation/revision date windows; bounded known-PMID reconciliation. Not a complete literature search.',
            'limitations': ['No full text is fetched or claims verified.',
                            'Modification dates describe completed-citation revisions; indexing and notice links can lag.',
                            'Known records are rechecked in bounded oldest-first batches; NLM deletions are not mirrored exhaustively.',
                            'Historical proposal evidence and reference snapshots are never replaced.']}


def search(data_dir, query, limit=5):
    """Search only downloaded public metadata; performs no network request."""
    if not isinstance(query, str) or len(query) > 1000:
        raise ValueError('Publication query must be text of at most 1,000 characters.')
    _integer(limit, 'limit', 1, 20)
    path = Path(data_dir) / 'publications.sqlite3'
    result = {'layer': 'publications', 'results': [], 'coverage': 'Downloaded public metadata and abstracts only; not an exhaustive literature search.',
              'retained_as_proposal_evidence': False,
              'reading_hint': 'Resolve the selected PMID and read the publication before retaining it as proposal evidence.'}
    if not path.is_file():
        return result
    tokens = re.findall(r'\w+', query, flags=re.UNICODE)[:30]
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if tokens:
            match = ' AND '.join('"' + word.replace('"', '""') + '"' for word in tokens)
            rows = conn.execute('''SELECT p.* FROM pubmed_fts f JOIN pubmed_records p ON p.pmid=f.pmid
                WHERE pubmed_fts MATCH ? ORDER BY rank LIMIT ?''', (match, limit)).fetchall()
        else:
            rows = conn.execute('SELECT * FROM pubmed_records ORDER BY retrieved_at DESC LIMIT ?', (limit,)).fetchall()
    for row in rows:
        snapshot = json.loads(row['record_json'])
        snapshot.update(last_checked_at=row['last_checked_at'], unavailable_at=row['unavailable_at'],
                        record_sha256=row['record_sha256'])
        if row['unavailable_at']:
            snapshot['limitations'].append('This PMID was absent from its latest public recheck; the retained older metadata may be unavailable or outdated.')
        result['results'].append(snapshot)
    return result
