"""Inventory, indexing and retrieval. Originals are only ever opened for reading."""
from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sqlite3
import time
from contextlib import contextmanager, closing
from collections import Counter
from datetime import datetime, timezone

from .extract import extract_file
from .locking import archive_writer
from .taxonomy import annotate, classify_document, expand_query, SYNONYM_GROUPS

SCHEMA_VERSION = 2
EXTRACTION_CACHE_VERSION = 2
TEXT_TYPES = {'.docx', '.doc', '.rtf', '.pdf', '.txt', '.md'}
EVIDENCE_STATUSES = {
    'proposed', 'reported_result_unverified', 'mixed_or_unclear', 'not_applicable',
    'published_result_verified', 'completed_result_confirmed',
}
ROLES = {'own_grant', 'collaborator_or_template', 'literature', 'ownership_uncertain'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def _source_options(source):
    """Validate optional source controls before touching any source files."""
    exclusions = source.get('exclude_paths', [])
    if not isinstance(exclusions, list):
        raise ValueError('Source exclude_paths must be a list of relative paths.')
    normalized = []
    for value in exclusions:
        if not isinstance(value, str) or not value.strip() or '\x00' in value:
            raise ValueError('Excluded paths must be nonempty relative paths.')
        value = value.replace('\\', '/')
        if (PurePosixPath(value).is_absolute() or PureWindowsPath(value).drive
                or any(part in ('.', '..') for part in value.split('/'))):
            raise ValueError('Excluded paths must be relative and cannot contain . or .. components.')
        normalized.append(PurePosixPath(value).as_posix())
    default_role = source.get('default_role')
    if default_role is not None and (not isinstance(default_role, str) or default_role not in ROLES):
        raise ValueError('Source default_role must be a known document role.')
    project_from_subfolder = source.get('project_from_subfolder', False)
    if not isinstance(project_from_subfolder, bool):
        raise ValueError('Source project_from_subfolder must be a boolean.')
    return list(dict.fromkeys(normalized)), default_role, project_from_subfolder


def _vocabulary(value):
    """Validate archive-specific topic rules and synonym groups."""
    if value is None:
        return {'topics': {}, 'synonyms': {}}
    if not isinstance(value, dict) or set(value) - {'topics', 'synonyms'}:
        raise ValueError('vocabulary must be an object with optional "topics" and "synonyms".')
    topics, synonyms = value.get('topics', {}), value.get('synonyms', {})
    if not isinstance(topics, dict) or len(topics) > 200:
        raise ValueError('vocabulary.topics must map at most 200 tags to regular expressions.')
    for tag, pattern in topics.items():
        if not re.fullmatch(r'[a-z0-9_]{1,60}', tag) or not isinstance(pattern, str) or len(pattern) > 1000:
            raise ValueError('vocabulary.topics tags must be short snake_case names mapped to patterns.')
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ValueError(f'vocabulary.topics.{tag} is not a valid regular expression: {exc}') from None
    if not isinstance(synonyms, dict) or len(synonyms) > 200:
        raise ValueError('vocabulary.synonyms must map at most 200 concepts to term lists.')
    for key, terms in synonyms.items():
        if (not re.fullmatch(r'[a-z0-9_]{1,60}', key) or not isinstance(terms, list) or not 1 <= len(terms) <= 20
                or any(not isinstance(t, str) or not t.strip() or len(t) > 120 for t in terms)):
            raise ValueError('vocabulary.synonyms must map snake_case concepts to 1..20 short terms.')
    return {'topics': dict(topics), 'synonyms': {k: [t.strip() for t in v] for k, v in synonyms.items()}}


def _synonym_groups(conn):
    row = conn.execute("SELECT value FROM metadata WHERE key='vocabulary'").fetchone()
    synonyms = json.loads(row[0]).get('synonyms', {}) if row else {}
    return tuple(tuple(' '.join(t.lower().split()) for t in terms) for terms in synonyms.values())


def load_config(path):
    path = Path(path).resolve()
    config = json.loads(path.read_text())
    if not config.get('sources') and config.get('archive_optional') is not True:
        raise ValueError('Configuration must contain at least one source directory.')
    if not isinstance(config.get('sources'), list):
        raise ValueError('Configuration sources must be a list.')
    names = set()
    roots = []
    for source in config['sources']:
        if not source.get('name') or source['name'] in names:
            raise ValueError('Source names must be present and unique.')
        names.add(source['name'])
        root = Path(source['path']).expanduser()
        if not root.is_absolute():
            root = path.parent / root
        source['path'] = str(root.resolve())
        exclusions, _, _ = _source_options(source)
        if 'exclude_paths' in source:
            source['exclude_paths'] = exclusions
        roots.append(root.resolve())
    data = Path(config.get('data_dir', '.local')).expanduser()
    if not data.is_absolute():
        data = path.parent / data
    data = data.resolve()
    if any(data == root or root in data.parents for root in roots):
        raise ValueError('The generated data directory must be outside the source directories.')
    config['data_dir'] = str(data)
    config['workers'] = max(1, min(int(config.get('workers', 4)), 8))
    config['vocabulary'] = _vocabulary(config.get('vocabulary'))
    return config


@contextmanager
def _connect(path):
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
    finally:
        connection.close()


def _schema(conn):
    conn.executescript('''
    CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE documents (
      id TEXT PRIMARY KEY, source_name TEXT, project TEXT, relative_path TEXT, source_path TEXT,
      title TEXT, extension TEXT, size_bytes INTEGER, modified_at TEXT,
      sha256 TEXT, role TEXT, cycle TEXT, kind TEXT,
      extraction_status TEXT, duplicate_of TEXT, unit_count INTEGER DEFAULT 0,
      warnings TEXT, review_flags TEXT, metadata TEXT
    );
    CREATE INDEX documents_project ON documents(project);
    CREATE TABLE units (
      id TEXT UNIQUE, document_id TEXT REFERENCES documents(id), ordinal INTEGER,
      text TEXT, locator TEXT, section TEXT, kind TEXT, categories TEXT,
      topics TEXT, techniques TEXT, models TEXT, evidence_status TEXT,
      review_flags TEXT, annotation_method TEXT, tags_text TEXT, text_hash TEXT,
      identifiers TEXT, review_note TEXT DEFAULT ''
    );
    CREATE INDEX units_document ON units(document_id);
    CREATE INDEX units_hash ON units(text_hash);
    CREATE VIRTUAL TABLE unit_fts USING fts5(text, section, tags_text,
      content='units', content_rowid='rowid', tokenize='unicode61 remove_diacritics 2');
    CREATE TABLE identifiers (
      identifier TEXT, kind TEXT, unit_id TEXT REFERENCES units(id),
      PRIMARY KEY(identifier, kind, unit_id)
    );
    ''')


def _ignored(path, relative):
    if path.is_symlink():
        return 'symlink_not_followed'
    if any(part.startswith('.') for part in relative.parts):
        return 'hidden_or_application_metadata'
    if path.name.startswith('~$') or path.name.endswith('.docx#'):
        return 'application_lock_file'
    return None


def _fingerprint(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_cached(path, digest, cache):
    entry = cache / f'{digest}{path.suffix.lower()}.json'
    if entry.exists():
        try:
            data = json.loads(entry.read_text())
            if (data.get('cache_version') == EXTRACTION_CACHE_VERSION
                    and data['extraction'].get('metadata', {}).get('status') not in ('error', 'no_text')):
                return data['extraction']
        except (OSError, ValueError, KeyError):
            pass
    result = extract_file(path)
    if result.get('metadata', {}).get('status') != 'error':
        _write_json(entry, {'cache_version': EXTRACTION_CACHE_VERSION, 'extraction': result})
    return result


_DOI_START = re.compile(r'\b10\.\d{4,9}/[^\s<>"\]\}]+', re.I)
_DOI_CONTINUATION = re.compile(r'[ \t]*\r?\n?[ \t]*([A-Za-z0-9(][^\s<>"\]\}]+)')
_DOI_SICI = re.compile(r'<\d+::AID-[^\s<>]+>[^\s<>"\]\}]*')
_DOI_URL_TAIL = re.compile(r'[&?#].*$')
_DOI_JUNK_TAIL = re.compile(
    r'(?:/-/dcsupplemental|/(?:full|pdf|epdf|abstract|fulltext|html|suppl_file)'
    r'|\.(?:epub|pmid|pmc|pubmed|accessed|available|doi|cited|retrieved))$', re.I)
_DOI_COMPONENT = re.compile(r'\.(?:g|t|s|sd|sg|st|e)\d{3}$', re.I)   # PLOS figure/table/supplement components
_DOI_PREPRINT_VERSION = re.compile(r'v\d+$', re.I)
_DOI_SKIPPED_PREFIXES = ('10.13039',)   # Crossref Funder Registry entries are not citable works


def clean_doi(value):
    """Canonical lowercase DOI from text found in a document, or None when it is junk or a truncated stub.

    Removes OpenURL/query tails, footnote digits after an unbalanced parenthesis, viewer suffixes
    (/full, .Epub, .PMID), publisher component suffixes, and preprint version markers.
    """
    value = _DOI_URL_TAIL.sub('', value.strip())
    while value.count(')') > value.count('(') and ')' in value:
        value = value[:value.rfind(')')]
    changed = True
    while changed:
        stripped = value.rstrip('.,;:')
        stripped = _DOI_JUNK_TAIL.sub('', stripped)
        stripped = _DOI_COMPONENT.sub('', stripped)
        if stripped.lower().startswith('10.1101/'):
            stripped = _DOI_PREPRINT_VERSION.sub('', stripped)
        changed, value = stripped != value, stripped
    prefix, _, suffix = value.partition('/')
    if prefix.lower().startswith(_DOI_SKIPPED_PREFIXES):
        return None
    if len(suffix) < 4 or suffix[-1] in '-/(.' or (len(suffix) < 8 and not any(c.isdigit() for c in suffix)):
        return None   # a stub left by a line break or a truncated extraction; never a resolvable work
    return value.lower()


def _join_split_doi(value, text, end):
    """Rejoin a DOI whose extraction broke after a hyphen at a line or column boundary."""
    if not value.endswith('-'):
        return value
    match = _DOI_CONTINUATION.match(text, end)
    if not match:
        return value
    rest = match.group(1)
    if len(rest.rstrip('.,;:')) < 3 or not any(c.isdigit() for c in rest):
        return value   # prose or the next reference's number, not the rest of a DOI
    # Letters on both sides mean the hyphen was inserted by hyphenation (j.cel-/rep); otherwise it belongs to the DOI.
    if value[-2].isalpha() and rest[0].isalpha():
        return value[:-1] + rest
    return value + rest


def _identifiers(text):
    # These are identifiers observed in source text, not verified bibliographic records.
    found = []
    for match in _DOI_START.finditer(text):
        value, end = match.group(0), match.end()
        value = _join_split_doi(value, text, end)
        sici = _DOI_SICI.match(text, match.end())
        if sici and not value.endswith('-'):
            value += sici.group(0)
        doi = clean_doi(value)
        if doi:
            found.append({'kind': 'doi', 'identifier': doi})
    for value in re.findall(r'\bPMID\s*[:#]?\s*(\d{5,9})\b', text, re.I):
        found.append({'kind': 'pmid', 'identifier': value})
    return [dict(kind=k, identifier=v) for k, v in sorted({(x['kind'], x['identifier']) for x in found})]


def _curation(data_dir):
    path = Path(data_dir) / 'curation.json'
    if path.exists():
        return json.loads(path.read_text())
    return {'documents': {}, 'units': {}}


def _walk_files(root, exclude_paths=()):
    def onerror(error):
        raise error
    excluded = tuple(Path(value) for value in exclude_paths)
    def is_excluded(path):
        relative = path.relative_to(root)
        return any(relative == item or item in relative.parents for item in excluded)
    for parent, folders, files in os.walk(root, followlinks=False, onerror=onerror):
        # Prune generated/excluded trees before descending or reading their contents.
        # Directory links are inventoried but never followed out of the source root.
        for folder in list(folders):
            path = Path(parent) / folder
            if is_excluded(path):
                folders.remove(folder)
            elif path.is_symlink():
                folders.remove(folder)
                yield path
        for filename in files:
            path = Path(parent) / filename
            if not is_excluded(path):
                yield path


def _prepare_units(units):
    """Bound long excerpts while keeping exact offsets into extracted source blocks."""
    section = ''
    previous_page = None
    heading = re.compile(r'^(?:[A-Z]|\d{1,2})?[.)\s]*'
        r'(SPECIFIC AIMS|SIGNIFICANCE|INNOVATION|BACKGROUND|PRELIMINARY (?:DATA|STUDIES|RESULTS)|'
        r'APPROACH|RESEARCH (?:STRATEGY|DESIGN)|METHODS|REFERENCES|BIBLIOGRAPHY|'
        r'POTENTIAL (?:PROBLEMS|PITFALLS)|STATISTICAL (?:ANALYSIS|DESIGN))\b', re.I)
    for original in units:
        item = dict(original)
        text = item.get('text', '').strip()
        if not text:
            continue
        body = item.get('kind') not in ('comment', 'footnote', 'endnote', 'header', 'footer')
        page = item.get('page')
        if page is not None and page != previous_page:
            # Full NIH application PDFs concatenate many independent attachments.
            # Do not carry a bibliography/section label into a different page's attachment.
            section = ''
            previous_page = page
        if body:
            match = heading.match(text[:150])
            if match:
                section = match.group(1).title()
                item['section'] = section
            elif item.get('section'):
                section = item['section']
            if section and not item.get('section'):
                item['section'] = section
                item['warnings'] = item.get('warnings', []) + ['section_inferred_from_heading']
        if len(text) <= 2400 or item.get('kind') in ('comment', 'table_row'):
            item['text'] = text
            yield item
            continue
        start = 0
        while start < len(text):
            end = min(start + 2200, len(text))
            if end < len(text):
                boundaries = list(re.finditer(r'(?:[.!?]\s+|\n)', text[start + 1200:end]))
                if boundaries:
                    end = start + 1200 + boundaries[-1].end()
            piece = dict(item)
            piece['text'] = text[start:end]
            piece['locator'] = item['locator'] + f'; excerpt chars {start + 1}-{end}'
            piece['warnings'] = item.get('warnings', []) + ['excerpt_offsets_reference_extracted_text']
            yield piece
            start = end


def ingest(config, progress=None, *, validate_snapshot=None):
    """Build an atomic index snapshot, caching extraction by file bytes and format."""
    # Fail before replacing a usable library when a configured drive is unavailable.
    for source in config['sources']:
        _source_options(source)
        if not Path(source['path']).is_dir():
            raise ValueError(f"Source is unavailable; existing index retained: {source['path']}")
    return _ingest(config, progress, validate_snapshot=validate_snapshot)


@archive_writer
def _ingest(config, progress=None, *, validate_snapshot=None):
    started = time.monotonic()
    data_dir = Path(config['data_dir'])
    data_dir.mkdir(parents=True, exist_ok=True)
    cache = data_dir / 'cache'
    cache.mkdir(exist_ok=True)
    curated = _curation(data_dir)
    records = []
    for source in config['sources']:
        root = Path(source['path'])
        exclusions, default_role, project_from_subfolder = _source_options(source)
        for path in sorted(_walk_files(root, exclusions)):
            rel = path.relative_to(root)
            meta = classify_document(str(rel))
            if default_role is not None and meta['role'] == 'own_grant':
                meta['role'] = default_role
                meta.setdefault('review_flags', []).append('document_role_from_source_default')
            project = rel.parts[0] if project_from_subfolder and len(rel.parts) > 1 else source['name']
            record = {
                'id': _id(source['name'] + '/' + str(rel)), 'source_name': source['name'],
                'project': project,
                'relative_path': str(rel), 'source_path': str(path.absolute()), 'title': path.name,
                'extension': path.suffix.lower(), 'size_bytes': 0, 'modified_at': '',
                'sha256': '', 'duplicate_of': None, 'unit_count': 0, 'warnings': [],
                'role': meta['role'], 'cycle': meta['cycle'], 'kind': meta['kind'],
                'review_flags': meta.get('review_flags', []), 'metadata': {},
            }
            for key, value in curated.get('documents', {}).get(record['id'], {}).items():
                if key in ('role', 'cycle', 'kind'):
                    record[key] = value
            ignored = _ignored(path, rel)
            try:
                stat = path.lstat()
                record['size_bytes'] = stat.st_size
                record['modified_at'] = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
                if ignored:
                    record['extraction_status'] = 'ignored'
                    record['warnings'].append(ignored)
                elif record['extension'] not in TEXT_TYPES:
                    record['extraction_status'] = 'asset_only'
                    record['warnings'].append('inventoried_only_no_content_extraction')
                else:
                    record['sha256'] = _fingerprint(path)
                    record['extraction_status'] = 'pending'
            except OSError as error:
                record['extraction_status'] = 'error'
                record['warnings'].append(f'{type(error).__name__}: {error}')
            records.append(record)
    # Prefer an own-grant occurrence, then an explicitly named revision. This chooses a
    # citation for byte-identical copies only; it does not establish a final version.
    pending = [r for r in records if r['extraction_status'] == 'pending']
    cycle_rank = {'resubmission': 0, 'unspecified': 1, 'historical': 2}
    pending.sort(key=lambda r: (r['role'] != 'own_grant', cycle_rank.get(r['cycle'], 5), r['relative_path']))
    by_hash = {}
    primary = []
    for record in pending:
        key = (record['sha256'], record['extension'])
        if key in by_hash:
            record['duplicate_of'] = by_hash[key]['id']
            record['extraction_status'] = 'duplicate'
        else:
            by_hash[key] = record
            primary.append(record)
    extractions = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=config.get('workers', 4)) as pool:
        futures = {pool.submit(_extract_cached, Path(r['source_path']), r['sha256'], cache): r for r in primary}
        for i, future in enumerate(concurrent.futures.as_completed(futures), 1):
            record = futures[future]
            try:
                result = future.result()
                record['warnings'].extend(result.get('warnings', []))
                record['metadata'] = result.get('metadata', {})
                result['units'] = list(_prepare_units(result.get('units', [])))
                record['unit_count'] = len(result['units'])
                record['extraction_status'] = (
                    ('parsed_with_warnings' if record['warnings'] else 'parsed')
                    if record['unit_count'] else 'needs_review'
                )
                if result.get('metadata', {}).get('status') == 'error':
                    record['extraction_status'] = 'error'
                extractions[record['id']] = result
            except Exception as error:
                record['extraction_status'] = 'error'
                record['warnings'].append(f'{type(error).__name__}: {error}')
            if progress and (i % 20 == 0 or i == len(primary)):
                progress(f'Extracted {i}/{len(primary)} distinct text files')
    for record in records:
        if record['duplicate_of']:
            primary_record = next(r for r in primary if r['id'] == record['duplicate_of'])
            record['warnings'].append('exact_byte_duplicate; see canonical document')
            record['metadata']['canonical_extraction_status'] = primary_record['extraction_status']
            record['unit_count'] = primary_record['unit_count']
            extractions[record['id']] = extractions.get(primary_record['id'], {})
    database = data_dir / 'library.sqlite3'
    temporary = data_dir / f'library.build-{os.getpid()}.sqlite3'
    if temporary.exists():
        temporary.unlink()
    conn = sqlite3.connect(temporary)
    try:
        _schema(conn)
        conn.execute('INSERT INTO metadata VALUES (?, ?)', ('schema_version', str(SCHEMA_VERSION)))
        conn.execute('INSERT INTO metadata VALUES (?, ?)', ('built_at', _now()))
        conn.execute('INSERT INTO metadata VALUES (?, ?)', ('sources', _json(config['sources'])))
        vocabulary = _vocabulary(config.get('vocabulary'))
        conn.execute('INSERT INTO metadata VALUES (?, ?)', ('vocabulary', _json(vocabulary)))
        for record in records:
            columns = list(record)
            conn.execute(
                f"INSERT INTO documents ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                [_json(v) if isinstance(v, (list, dict)) else v for v in record.values()],
            )
            extraction = extractions.get(record['id'], {})
            for ordinal, unit in enumerate(extraction.get('units', []), 1):
                text = unit.get('text', '').strip()
                if not text:
                    continue
                section = unit.get('section', '')
                context = {**record, 'kind': 'reviewer_feedback'} if unit.get('kind') == 'comment' else record
                labels = annotate(text, section=section, document=context, topic_rules=vocabulary['topics'])
                uid = _id(record['id'] + ':' + unit.get('locator', str(ordinal)) + ':' + text)
                overrides = curated.get('units', {}).get(uid, {})
                for key in ('categories', 'topics', 'techniques', 'models', 'evidence_status', 'review_note'):
                    if key in overrides:
                        labels[key] = overrides[key]
                if overrides:
                    labels['annotation_method'] = 'rules_v1_with_manual_review'
                ids = _identifiers(text)
                values = {
                    'id': uid, 'document_id': record['id'], 'ordinal': ordinal,
                    'text': text, 'locator': unit.get('locator', f'unit {ordinal}'), 'section': section,
                    'kind': unit.get('kind', 'paragraph'),
                    **{k: labels.get(k, []) for k in ('categories', 'topics', 'techniques', 'models')},
                    'evidence_status': labels.get('evidence_status', 'mixed_or_unclear'),
                    'review_flags': list(dict.fromkeys(labels.get('review_flags', []) + unit.get('warnings', []))),
                    'annotation_method': labels.get('annotation_method', 'rules_v1'),
                    'tags_text': ' '.join(v for k in ('categories', 'topics', 'techniques', 'models') for v in labels.get(k, [])),
                    'text_hash': _id(' '.join(text.lower().split())), 'identifiers': ids,
                    'review_note': labels.get('review_note', ''),
                }
                conn.execute(
                    f"INSERT INTO units ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",
                    [_json(v) if isinstance(v, (list, dict)) else v for v in values.values()],
                )
                for identifier in ids:
                    conn.execute('INSERT OR IGNORE INTO identifiers VALUES (?, ?, ?)',
                                 (identifier['identifier'], identifier['kind'], uid))
        conn.execute("INSERT INTO unit_fts(unit_fts) VALUES ('rebuild')")
        conn.commit()
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('SQLite integrity check failed; previous library retained.')
        if validate_snapshot is not None:
            validate_snapshot(records)
    except Exception:
        conn.close()
        temporary.unlink(missing_ok=True)
        raise
    conn.close()
    temporary.replace(database)
    _write_json(data_dir / 'inventory.json', {'built_at': _now(), 'documents': records})
    if not (data_dir / 'curation.json').exists():
        _write_json(data_dir / 'curation.json', curated)
    result = stats(database)
    result['elapsed_seconds'] = round(time.monotonic() - started, 1)
    _write_json(data_dir / 'coverage.json', result)
    write_coverage(database, data_dir / 'coverage.md')
    return result


def _decode_document(row):
    result = dict(row)
    for key in ('warnings', 'review_flags', 'metadata'):
        result[key] = json.loads(result.get(key) or ('{}' if key == 'metadata' else '[]'))
    result['source_uri'] = Path(result['source_path']).as_uri()
    return result


def _decode_unit(row):
    result = dict(row)
    for key in ('categories', 'topics', 'techniques', 'models', 'review_flags', 'identifiers'):
        result[key] = json.loads(result.get(key) or '[]')
    if result.get('source_path'):
        result['source_uri'] = Path(result['source_path']).as_uri()
    return result


def stats(db_path):
    with _connect(db_path) as conn:
        counts = {
            'documents': conn.execute('SELECT count(*) FROM documents').fetchone()[0],
            'units': conn.execute('SELECT count(*) FROM units').fetchone()[0],
            'unique_passages': conn.execute('SELECT count(DISTINCT text_hash) FROM units').fetchone()[0],
            'unique_identifiers': conn.execute("SELECT count(*) FROM (SELECT DISTINCT identifier,kind FROM identifiers)").fetchone()[0],
        }
        for row in conn.execute('SELECT extraction_status,count(*) n FROM documents GROUP BY extraction_status'):
            counts[row[0]] = row[1]
        facets = {}
        for key, col in [('category','categories'), ('topic','topics'), ('technique','techniques')]:
            facets[key] = [dict(value=r[0], count=r[1]) for r in conn.execute(
                f'SELECT j.value,count(*) FROM units u,json_each(u.{col}) j GROUP BY j.value ORDER BY count(*) DESC')]
        for key in ('role', 'cycle', 'project'):
            facets[key] = [dict(value=r[0], count=r[1]) for r in conn.execute(
                f'SELECT {key},count(*) FROM documents GROUP BY {key} ORDER BY {key}')]
        facets['status'] = [dict(value=r[0], count=r[1]) for r in conn.execute(
            'SELECT evidence_status,count(*) FROM units GROUP BY evidence_status ORDER BY evidence_status')]
        warnings = [dict(value=r[0], count=r[1]) for r in conn.execute(
            'SELECT j.value,count(*) FROM documents d,json_each(d.warnings) j GROUP BY j.value ORDER BY count(*) DESC')]
        metadata = {r[0]: r[1] for r in conn.execute('SELECT key,value FROM metadata')}
        return {'counts': counts, 'facets': facets, 'warnings': warnings, 'built_at': metadata.get('built_at'),
                'search_mode': 'keyword_with_synonyms', 'annotation_method': 'rules_v1',
                'sources': json.loads(metadata.get('sources', '[]'))}


UNIT_SELECT = '''u.*, d.source_path, d.relative_path, d.title, d.role, d.cycle, d.project,
                 d.kind AS document_kind, d.extraction_status'''


def search(db_path, query, filters=None, limit=30):
    filters = dict(filters or {})
    limit = max(1, min(int(limit), 200))
    clauses, args = [], []
    role = filters.get('role', 'own_grant')
    if role and role != 'all':
        clauses.append('d.role = ?')
        args.append(role)
    if filters.get('cycle'):
        clauses.append('d.cycle = ?')
        args.append(filters['cycle'])
    if filters.get('project') and filters['project'] != 'all':
        clauses.append('d.project = ?')
        args.append(filters['project'])
    if filters.get('status'):
        clauses.append('u.evidence_status = ?')
        args.append(filters['status'])
    for key, column in [('category','categories'), ('topic','topics'), ('technique','techniques')]:
        if filters.get(key):
            clauses.append(f'EXISTS (SELECT 1 FROM json_each(u.{column}) WHERE value = ?)')
            args.append(filters[key])
    stopwords = {'a', 'an', 'the', 'of', 'in', 'for', 'to', 'and', 'or', 'my', 'our',
                 'we', 'what', 'which', 'show', 'find', 'me', 'about', 'is', 'are', 'with', 'on', 'from'}
    tokens = [t for t in re.findall(r'[\w]+(?:[-][\w]+)*', query.lower()) if t not in stopwords][:16]
    with _connect(db_path) as conn:
        extra_groups = _synonym_groups(conn)
    expanded = expand_query(query, extra_groups) if query.strip() else []
    if expanded and any(expanded[0] in group for group in (*SYNONYM_GROUPS, *extra_groups)):
        tokens = [expanded[0]]
    fts_groups = []
    for token in tokens:
        terms = expand_query(token, extra_groups)
        fts_groups.append('(' + ' OR '.join('"' + t.replace('"', '""') + '"' for t in terms) + ')')
    joined = ' JOIN unit_fts ON unit_fts.rowid=u.rowid' if fts_groups else ''
    if fts_groups:
        clauses.insert(0, 'unit_fts MATCH ?')
        args.insert(0, ' AND '.join(fts_groups))
    elif query.strip():
        return []
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    cycle_order = "CASE d.cycle WHEN 'resubmission' THEN 0 WHEN 'unspecified' THEN 1 WHEN 'historical' THEN 2 ELSE 3 END"
    order = 'bm25(unit_fts, 1.0, 0.6, 0.5), ' + cycle_order if fts_groups else cycle_order + ',u.ordinal'
    with _connect(db_path) as conn:
        rows = conn.execute(f'SELECT {UNIT_SELECT} FROM units u JOIN documents d ON d.id=u.document_id{joined}{where} ORDER BY {order}', args)
        results, seen = [], set()
        for row in rows:
            item = _decode_unit(row)
            if item['text_hash'] in seen:
                continue
            seen.add(item['text_hash'])
            item['also_found_in_count'] = conn.execute(
                'SELECT count(DISTINCT document_id)-1 FROM units WHERE text_hash=?', (item['text_hash'],)).fetchone()[0]
            results.append(item)
            if len(results) >= limit:
                break
        return results


def list_documents(db_path, filters=None, limit=1000):
    clauses, args = [], []
    for key in ('role', 'cycle', 'project', 'extraction_status'):
        if (filters or {}).get(key) and filters[key] != 'all':
            clauses.append(key + '=?')
            args.append(filters[key])
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    args.append(max(1, min(int(limit), 10000)))
    with _connect(db_path) as conn:
        return [_decode_document(r) for r in conn.execute('SELECT * FROM documents' + where + ' ORDER BY relative_path LIMIT ?', args)]


def get_document(db_path, doc_id):
    with _connect(db_path) as conn:
        row = conn.execute('SELECT * FROM documents WHERE id=?', (doc_id,)).fetchone()
        if row is None:
            raise KeyError('Document not found')
        result = _decode_document(row)
        result['units'] = [_decode_unit(r) for r in conn.execute(
            f'SELECT {UNIT_SELECT} FROM units u JOIN documents d ON d.id=u.document_id WHERE u.document_id=? ORDER BY u.ordinal', (doc_id,))]
        return result


@archive_writer
def curate_unit(db_path, unit_id, status, note):
    if status not in EVIDENCE_STATUSES:
        raise ValueError('Unknown evidence status')
    if not note.strip():
        raise ValueError('A review note describing the evidence or correction is required.')
    with _connect(db_path) as conn:
        if not conn.execute('SELECT 1 FROM units WHERE id=?', (unit_id,)).fetchone():
            raise ValueError('Unknown unit ID')
    data_dir = Path(db_path).parent
    data = _curation(data_dir)
    data.setdefault('units', {})[unit_id] = {'evidence_status': status, 'review_note': note, 'reviewed_at': _now()}
    _write_json(data_dir / 'curation.json', data)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute('UPDATE units SET evidence_status=?,review_note=?,annotation_method=? WHERE id=?',
                     (status, note, 'rules_v1_with_manual_review', unit_id))


@archive_writer
def curate_document(db_path, doc_id, role):
    if role not in ROLES:
        raise ValueError('Unknown document role')
    get_document(db_path, doc_id)
    data_dir = Path(db_path).parent
    data = _curation(data_dir)
    data.setdefault('documents', {}).setdefault(doc_id, {})['role'] = role
    _write_json(data_dir / 'curation.json', data)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute('UPDATE documents SET role=? WHERE id=?', (role, doc_id))


def write_coverage(db_path, path):
    report = stats(db_path)
    lines = ['# Grant library coverage', '', f"Built: {report['built_at']}", '',
             'This is a text index with rule-based candidate annotations. Source claims and references have not been scientifically verified.', '',
             '## Inventory', '']
    for key, value in report['counts'].items():
        lines.append(f'- {key.replace("_", " ")}: {value}')
    lines += ['', '## Source roots', '']
    for source in report['sources']:
        lines.append(f"- {source['name']}: {source['path']}")
    lines += ['', '## Coverage limits', '',
              '- PDF pages with little extractable text are flagged. OCR is not implemented.',
              '- Embedded figures are inventoried; their visual claims are not interpreted.',
              '- Spreadsheets, slides, images, archives, and proprietary figure files are inventoried only.',
              '- Word locators identify XML paragraphs/tables, not rendered page numbers.',
              '- Bibliographic identifiers are observed strings, not verified reference records.',
              '- Exact byte duplicates are linked; repeated passages are collapsed in search, not treated as independent evidence.',
              '- Filename-derived ownership and submission cycles need review. File modification dates do not establish a final submission.',
              '- All extraction and annotation occurs locally; no model or remote embedding API is called.',
              '', '## Documents requiring attention', '']
    for document in list_documents(db_path, limit=10000):
        if document['extraction_status'] in ('error', 'needs_review', 'parsed_with_warnings'):
            lines.append(f"- **{document['relative_path']}** ({document['extraction_status']}): {'; '.join(document['warnings'])}")
    Path(path).write_text('\n'.join(lines) + '\n')


def write_brief(db_path, query, output, filters=None, limit=30):
    matches = search(db_path, query, filters, limit)
    lines = [f'# Source brief: {query}', '',
             'Retrieved source passages for drafting. These excerpts are not a verified synthesis. Check context, current literature, and evidence status before reusing claims.', '']
    for i, item in enumerate(matches, 1):
        lines += [f"## {i}. {item['title']} — {item['locator']}", '',
                  f"Source: [{item['relative_path']}]({item['source_uri']})", '',
                  f"Role: {item['role']} · Cycle: {item['cycle']} · Evidence: {item['evidence_status']}", '',
                  f"Topics: {', '.join(item['topics']) or 'unassigned'}", '',
                  f"Techniques: {', '.join(item['techniques']) or 'unassigned'}", '',
                  '\n'.join('> ' + line for line in item['text'].splitlines()), '',
                  f"Record ID: `{item['id']}`", '']
        if item['review_note']:
            lines += ['Review note: ' + item['review_note'], '']
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('\n'.join(lines) + '\n')
    return len(matches)
