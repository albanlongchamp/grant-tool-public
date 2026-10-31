"""Scheduled maintenance of mutable discovery indexes, never saved draft evidence.

The state file distinguishes an attempt, a complete refresh, and partial coverage.
No archive text or proposal brief is used to construct a public query.
"""
from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from . import library
from .locking import exclusive, BusyError

PUBLIC_LAYERS = ('publications', 'nih', 'guidance')


def _now():
    return datetime.now(timezone.utc)


def _iso(now):
    return now.isoformat()


def _age(value, now):
    return (now - datetime.fromisoformat(value)).total_seconds() if value else None


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _directory(data_dir):
    path = Path(data_dir) / 'refresh'
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _read(data_dir):
    path = Path(data_dir) / 'refresh/state.json'
    return json.loads(path.read_text()) if path.exists() else {'version': 1, 'layers': {}}


def _save(data_dir, state):
    path = _directory(data_dir) / 'state.json'
    temp = path.with_name(uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x') as handle:
            os.chmod(temp, 0o600)
            json.dump(state, handle, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def status(data_dir, *, now=None):
    """No network: freshness is based on successful coverage, not job start time."""
    now = now or _now()
    state = _read(data_dir)
    layers = {}
    for name in ('archive', *PUBLIC_LAYERS):
        value = state.get('layers', {}).get(name, {})
        age = _age(value.get('last_success'), now)
        # Archive watches every five minutes; public sources refresh weekly.
        age_limit = 900 if name == 'archive' else 8 * 86400
        stale = age is None or age > age_limit or value.get('status') != 'complete'
        layers[name] = {k: v for k, v in value.items() if k not in ('checkpoint', 'manifest')}
        layers[name].update(stale=stale, seconds_since_success=round(age) if age is not None else None)
        layers[name].setdefault('status', 'never_refreshed')
    return {'as_of': _iso(now), 'canonical_host': state.get('canonical_host'), 'layers': layers,
            'policy': 'Search stored sources; refresh public collections weekly; verify official requirements live.',
            'snapshots': 'Refreshes never rewrite saved proposal evidence, reference records or drafts.'}


def run(config, layers=PUBLIC_LAYERS, *, due=False, now=None):
    """Run configured public adapters under a single maintenance lock."""
    now = now or _now()
    if not layers or any(name not in PUBLIC_LAYERS for name in layers):
        raise ValueError('Refresh layers must be publications, nih, or guidance.')
    settings = config.get('refresh', {})
    data_dir = config['data_dir']
    if settings.get('enabled') is not True:
        raise ValueError('Enable refresh in the host config before scheduling public requests.')
    from . import publication_refresh, nih_refresh, guidance_refresh
    adapters = {'publications': publication_refresh.refresh, 'nih': nih_refresh.refresh,
                'guidance': guidance_refresh.refresh}
    # Scheduled public work waits its turn instead of skipping the only daily
    # check when an archive worker holds the lock. The supervisor bounds this
    # wait; archive ticks remain nonblocking and cannot starve the queued job.
    with exclusive(data_dir, 'refresh', wait=True):
        state = _read(data_dir)
        state['canonical_host'] = settings.get('canonical_host')
        results = {}
        for name in layers:
            previous = state['layers'].get(name, {})
            age = _age(previous.get('last_success'), now)
            settings_hash = _hash(settings.get(name, {}))
            due_date = (datetime.fromisoformat(previous['last_success']).date() + timedelta(days=7)
                        if previous.get('last_success') else None)
            if (due and due_date is not None and now.date() < due_date and previous.get('status') == 'complete'
                    and previous.get('settings_sha256') == settings_hash):
                results[name] = {'status': 'not_due'}
                continue
            attempt = {**previous, 'last_attempt': _iso(now), 'status': 'running', 'error': None,
                       'settings_sha256': settings_hash}
            state['layers'][name] = attempt
            _save(data_dir, state)
            try:
                result = adapters[name](data_dir, settings.get(name, {}), previous.get('checkpoint'))
                if result.get('status') not in ('complete', 'partial'):
                    raise ValueError('Adapter returned an invalid completion status.')
                attempt.update(result)
                attempt['finished_at'] = _iso(_now())
                if result['status'] == 'complete':
                    attempt['last_success'] = attempt['finished_at']
            except Exception as error:
                attempt.update(status='failed', finished_at=_iso(_now()),
                               error=f'{type(error).__name__}: {str(error)[:1000]}')
            _save(data_dir, state)
            results[name] = {k: v for k, v in attempt.items() if k != 'checkpoint'}
        return results


def _inventory(config, *, full=False):
    entries, content = [], []
    for source in config['sources']:
        root = Path(source['path'])
        if not root.is_dir():
            raise ValueError(f'Source unavailable; existing index retained: {source["name"]}')
        exclusions, _, _ = library._source_options(source)
        for path in sorted(library._walk_files(root, exclusions)):
            relative = path.relative_to(root)
            if library._ignored(path, relative):
                continue
            info = path.stat()
            key = source['name'] + '/' + str(relative)
            entries.append([key, info.st_size, info.st_mtime_ns, info.st_ctime_ns])
            if full and path.suffix.lower() in library.TEXT_TYPES:
                content.append([key, library._fingerprint(path)])
    controls = {'sources': config['sources'], 'curation': library._curation(Path(config['data_dir'])),
                'extraction_version': library.EXTRACTION_CACHE_VERSION, 'schema': library.SCHEMA_VERSION}
    return {'quick': _hash([entries, controls]), 'content': _hash(content) if full else None,
            'files': len(entries)}


def watch(config, *, force=False, now=None):
    """One watcher tick. Metadata scan each tick, daily content-hash backstop.

    Debouncing uses persisted observations, so launchd needn't hold a worker asleep.
    Rebuilds reuse content-addressed extraction caches and replace the index atomically.
    """
    now = now or _now()
    data_dir = config['data_dir']
    settings = config.get('refresh', {})
    if settings.get('enabled') is not True:
        raise ValueError('Enable refresh in the canonical host configuration first.')
    with exclusive(data_dir, 'refresh'):
        state = _read(data_dir)
        state['canonical_host'] = settings.get('canonical_host')
        prior = state['layers'].get('archive', {})
        current = {**prior, 'last_attempt': _iso(now), 'status': 'running', 'error': None}
        state['layers']['archive'] = current
        _save(data_dir, state)
        try:
            full = force or not prior.get('last_full_scan') or _age(prior['last_full_scan'], now) >= 86400
            scan = _inventory(config, full=full)
            previous_manifest = prior.get('manifest', {})
            pending = prior.get('pending') or {}
            changed = (force or scan['quick'] != previous_manifest.get('quick')
                       or (full and scan['content'] != previous_manifest.get('content'))
                       or bool(pending))
            if changed and not force:
                if pending.get('quick') != scan['quick'] or (full and pending.get('content') != scan['content']):
                    pending = {**scan, 'since': _iso(now)}
                settle_seconds = int(settings.get('settle_seconds', 60))
                if _age(pending['since'], now) < max(1, settle_seconds):
                    current.update(status='pending', pending=pending,
                                   warnings=['Source changes observed; waiting for a stable scan before publishing.'])
                    _save(data_dir, state)
                    return current
            if changed:
                old_errors = set()
                old_counts = {}
                database = Path(data_dir) / 'library.sqlite3'
                if database.exists():
                    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as conn:
                        old_errors = {row[0] for row in conn.execute("SELECT id FROM documents WHERE extraction_status='error'")}
                        old_counts = dict(conn.execute("SELECT source_name,count(*) FROM documents WHERE extraction_status!='ignored' GROUP BY source_name"))

                def validate(records):
                    after = _inventory(config, full=full)
                    if scan != after:
                        raise ValueError('Sources changed during indexing; previous index retained for retry.')
                    if any(r['extraction_status'] == 'error' and r['id'] not in old_errors for r in records):
                        raise ValueError('New file extraction errors; previous index retained. Inspect sources and retry.')
                    for source_name, count in old_counts.items():
                        if count and not any(r['source_name'] == source_name and r['extraction_status'] != 'ignored' for r in records):
                            raise ValueError('A previously populated source became empty; previous index retained. Check the mounted source or explicitly reconfigure it.')

                report = library.ingest(config, validate_snapshot=validate)
                current['counts'] = report['counts']
                current['index_built_at'] = report.get('built_at')
                # Keep the manifest validated before publication. A new scan here could
                # accidentally acknowledge bytes changed just after the index was built.
                current['warnings'] = ['Native Google Docs are indexed from local exports; cloud edits require an updated export.']
                current['pending'] = None
            else:
                current['warnings'] = prior.get('warnings', [])
            if not full:
                scan['content'] = previous_manifest.get('content')
            current.update(status='complete', last_success=_iso(_now()), manifest=scan,
                           changed=changed, finished_at=_iso(_now()))
            if full:
                current['last_full_scan'] = _iso(now)
        except Exception as error:
            current.update(status='failed', finished_at=_iso(_now()),
                           error=f'{type(error).__name__}: {str(error)[:1000]}')
        _save(data_dir, state)
        return {k: v for k, v in current.items() if k != 'manifest'}
