"""Immutable public article snapshots and explicitly retained exact paragraphs.

Whole articles are available only through the local reader. Proposal summaries,
claim judgments and model packets receive at most deliberately retained passages.
Retrieval and retention do not establish scientific support.
"""
from copy import deepcopy
from datetime import datetime, timezone
import base64
import hashlib
import json
import re
import uuid


MAX_DOCUMENTS = 20
MAX_PASSAGES = 100
MAX_SNAPSHOT_BYTES = 12 * 1024 * 1024
MAX_PARAGRAPH_CHARS = 12000
DOCUMENT_ID = re.compile(r'FT_[a-f0-9]{32}')
_HEAD_FIELDS = ('status', 'requested_pmcid', 'canonical', 'article_version', 'article_type', 'title',
                'authors', 'journal', 'year', 'provider', 'source_url', 'retrieved_at',
                'license', 'publication_notices', 'provenance', 'coverage', 'limitations')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS publication_documents (
      id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
      snapshot TEXT NOT NULL, summary TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS publication_passages (
      id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
      document_id TEXT NOT NULL REFERENCES publication_documents(id),
      paragraph_id TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(document_id,paragraph_id));
    CREATE INDEX IF NOT EXISTS publications_by_proposal ON publication_documents(proposal_id);
    CREATE INDEX IF NOT EXISTS publication_passages_by_proposal ON publication_passages(proposal_id);
    ''')


def _snapshot(value):
    try:
        encoded = _json(value)
    except (ValueError, TypeError):
        raise ValueError('The public article snapshot must contain JSON-safe values.') from None
    if len(encoded.encode('utf-8')) > MAX_SNAPSHOT_BYTES:
        raise ValueError('The public article snapshot exceeds the local storage limit.')
    value = json.loads(encoded)
    if (not isinstance(value, dict) or value.get('status') != 'retrieved'
            or not isinstance(value.get('title'), str) or not value['title'].strip()
            or not isinstance(value.get('canonical'), dict)
            or not isinstance(value.get('license'), dict)
            or value['license'].get('open_access') is not True
            or not value['license'].get('code')):
        raise ValueError('Retain only an identified, accessible public article with recorded open-access terms.')
    paragraphs, provenance = value.get('paragraphs'), value.get('provenance')
    if not isinstance(paragraphs, list) or not 1 <= len(paragraphs) <= 2000 or not isinstance(provenance, dict):
        raise ValueError('The article must contain a bounded paragraph extraction and provenance.')
    ids, characters = set(), 0
    for paragraph in paragraphs:
        if (not isinstance(paragraph, dict) or not isinstance(paragraph.get('id'), str)
                or not re.fullmatch(r'p[0-9]{4,}', paragraph['id']) or paragraph['id'] in ids
                or paragraph.get('kind') not in ('abstract', 'body')
                or not isinstance(paragraph.get('text'), str) or not paragraph['text'].strip()
                or paragraph.get('text_sha256') != _sha(paragraph['text'].encode('utf-8'))
                or any(not isinstance(paragraph.get(k), str) for k in ('section', 'locator', 'xml_path'))):
            raise ValueError('Article paragraph identities, exact text hashes and locators must be valid and unique.')
        ids.add(paragraph['id'])
        characters += len(paragraph['text'])
    if characters > 500000:
        raise ValueError('The extracted article text exceeds the local reader limit.')
    try:
        xml = base64.b64decode(value.get('raw_xml_b64', ''), validate=True)
    except (ValueError, TypeError):
        raise ValueError('The article lacks its exact encoded XML audit snapshot.') from None
    if not xml or len(xml) > 5 * 1024 * 1024 or _sha(xml) != provenance.get('xml_sha256'):
        raise ValueError('The article XML snapshot does not match its recorded hash or size bound.')
    if len(_json({k: value.get(k) for k in _HEAD_FIELDS}).encode('utf-8')) > 100000:
        raise ValueError('The article metadata exceeds the local summary limit.')
    return value


def summaries(connection, proposal_id):
    return [json.loads(row['summary']) for row in connection.execute(
        'SELECT summary FROM publication_documents WHERE proposal_id=? ORDER BY created_at DESC,rowid DESC',
        (proposal_id,))]


def passages(connection, proposal_id):
    return [json.loads(row['source']) for row in connection.execute(
        'SELECT source FROM publication_passages WHERE proposal_id=? ORDER BY created_at DESC,rowid DESC',
        (proposal_id,))]


def add_document(store, proposal_id, expected_revision, snapshot):
    value = _snapshot(snapshot)
    identifier, created = 'FT_' + uuid.uuid4().hex, _now()
    head = {key: deepcopy(value.get(key)) for key in _HEAD_FIELDS}
    head.update(id=identifier, proposal_id=proposal_id, created_at=created,
                paragraph_count=len(value['paragraphs']))
    with store.connect(True) as connection:
        store._editable(connection, proposal_id, expected_revision)
        if connection.execute('SELECT COUNT(*) FROM publication_documents WHERE proposal_id=?',
                              (proposal_id,)).fetchone()[0] >= MAX_DOCUMENTS:
            raise ValueError('This proposal already has 20 public article snapshots.')
        connection.execute('INSERT INTO publication_documents VALUES (?,?,?,?,?)',
                           (identifier, proposal_id, _json(value), _json(head), created))
        store._bump(connection, proposal_id)
    return {'proposal': store.get(proposal_id), 'document_id': identifier}


def _document(connection, proposal_id, document_id):
    if not isinstance(document_id, str) or not DOCUMENT_ID.fullmatch(document_id):
        raise ValueError('Select a public article snapshot from this proposal.')
    row = connection.execute('SELECT * FROM publication_documents WHERE proposal_id=? AND id=?',
                             (proposal_id, document_id)).fetchone()
    if row is None:
        raise ValueError('This article snapshot does not belong to the selected proposal.')
    return row


def reader(store, proposal_id, document_id):
    with store.connect() as connection:
        store._row(connection, proposal_id)
        row = _document(connection, proposal_id, document_id)
        value = json.loads(row['snapshot'])
        result = json.loads(row['summary'])
        result['paragraphs'] = value['paragraphs']
        return result


def _source(document, paragraph, identifier, created, actor):
    metadata = {key: deepcopy(document.get(key)) for key in
                ('canonical', 'authors', 'author_parts', 'journal', 'journal_abbreviation', 'year',
                 'volume', 'issue', 'pages', 'retrieved_at', 'provider',
                 'article_version', 'article_type', 'license', 'publication_notices', 'provenance', 'coverage', 'limitations')}
    metadata.update(role='literature', evidence_status='publication_not_independently_verified',
                    scope='abstract' if paragraph['kind'] == 'abstract' else 'full_text_excerpt',
                    locator=paragraph['locator'], section=paragraph['section'],
                    document_id=document['id'], paragraph_id=paragraph['id'],
                    xml_path=paragraph['xml_path'], element_id=paragraph.get('element_id'),
                    text_sha256=paragraph['text_sha256'], retained_at=created, retained_by=actor,
                    extractor_version=(document.get('provenance') or {}).get('extractor_version'),
                    notices=deepcopy(document.get('publication_notices')), truncated=False,
                    total_text_chars=len(paragraph['text']), included_text_chars=len(paragraph['text']))
    metadata['limitations'] = list(metadata.get('limitations') or []) + [
        'Only this exact retained extracted paragraph is supplied; other article paragraphs are outside this source.',
        'Retention is not a claim-support judgment, independent scientific verification, or a PDF transcription.']
    return {'id': identifier, 'layer': 'publication', 'title': document['title'],
            'text': paragraph['text'], 'source_uri': document.get('source_url', ''), 'metadata': metadata}


def retain(store, proposal_id, expected_revision, document_id, paragraph_id, *, actor='assistant'):
    if actor not in ('assistant', 'investigator'):
        raise ValueError('Choose the assistant or investigator who actually retained this paragraph.')
    with store.connect(True) as connection:
        store._editable(connection, proposal_id, expected_revision)
        row = _document(connection, proposal_id, document_id)
        document = json.loads(row['snapshot'])
        paragraph = next((p for p in document['paragraphs'] if p['id'] == paragraph_id), None)
        if paragraph is None:
            raise ValueError('Choose an exact paragraph from this article snapshot.')
        if len(paragraph['text']) > MAX_PARAGRAPH_CHARS:
            raise ValueError('This paragraph exceeds 12,000 characters and cannot be retained whole; no text was truncated.')
        if not connection.execute('SELECT 1 FROM publication_passages WHERE document_id=? AND paragraph_id=?',
                                  (document_id, paragraph_id)).fetchone():
            if connection.execute('SELECT COUNT(*) FROM publication_passages WHERE proposal_id=?',
                                  (proposal_id,)).fetchone()[0] >= MAX_PASSAGES:
                raise ValueError('This proposal already has 100 retained publication paragraphs.')
            identifier, created = 'F_' + uuid.uuid4().hex, _now()
            document['id'] = document_id
            source = _source(document, paragraph, identifier, created, actor)
            connection.execute('INSERT INTO publication_passages VALUES (?,?,?,?,?,?)',
                               (identifier, proposal_id, document_id, paragraph_id, _json(source), created))
            store._bump(connection, proposal_id)
    return store.get(proposal_id)


def source_notes(source, *, include_text=False):
    """Literal export appendix for retained paragraphs only, never full XML."""
    metadata = source.get('metadata') or {}
    if not metadata.get('document_id') or source.get('layer') != 'publication':
        return ''
    def literal(value):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        fence = '`' * max(3, max((len(run) for run in re.findall(r'`+', text)), default=0) + 1)
        return fence + 'text\n' + text + '\n' + fence
    details = {key: metadata.get(key) for key in
               ('scope', 'canonical', 'authors', 'journal', 'year', 'provider',
                'article_version', 'article_type', 'document_id', 'paragraph_id', 'locator',
                'section', 'xml_path', 'element_id', 'text_sha256', 'retrieved_at', 'retained_at',
                'retained_by', 'license', 'provenance', 'publication_notices', 'coverage', 'limitations')}
    lines = ['Retained publication paragraph only; retrieval is not scientific verification.']
    if include_text:
        lines += ['Exact retained extracted text:', literal(source['text'])]
    lines += ['Publication provenance and reading scope:', literal(details)]
    return '\n\n'.join(lines)
