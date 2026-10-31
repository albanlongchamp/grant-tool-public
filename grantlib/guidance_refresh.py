"""Versioned public guidance downloads, separate from curated advice and drafts.

HTTP validators follow RFC 9110 sections 13.1.2/13.1.3. A successful download
establishes retrieval freshness, never interpretation or compliance. No model
calls, private-source reads, or edits to saved proposal evidence occur here.
Downloads are limited to 32 MiB, allowing large official reports
while retaining a hard bound; returned text windows remain at most 20,000 chars.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import http.client
import ipaddress
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time
from urllib.parse import urljoin, urlsplit, urlunsplit

from .guidance import load_guidance, OFFICIAL_AGENCY_DOMAINS


DB_NAME = 'guidance_refresh.sqlite3'
OFFICIAL_DOMAINS = frozenset(OFFICIAL_AGENCY_DOMAINS.values()) | {'grants.gov'}
MAX_BYTES = 32 * 1024 * 1024
REQUEST_SECONDS = 25
MAX_REDIRECTS = 5


def _now():
    return datetime.now(timezone.utc).isoformat()


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def validate_url(url):
    """Canonicalize a public, explicitly supported government HTTPS URL."""
    if not isinstance(url, str) or not url or len(url) > 4096:
        raise ValueError('Official source URL must contain 1–4096 characters.')
    if any(ord(c) < 33 or ord(c) > 126 for c in url) or '\\' in url:
        raise ValueError('Official source URL must be ASCII with spaces encoded.')
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    if (parts.scheme != 'https' or parts.username is not None or parts.password is not None
            or parts.port not in (None, 443) or not re.fullmatch(r'[a-z0-9.-]+', host)
            or not any(host == domain or host.endswith('.' + domain) for domain in OFFICIAL_DOMAINS)):
        raise ValueError('Only public HTTPS NIH, NSF, HRSA, CDC, or Grants.gov URLs on port 443 are supported.')
    return urlunsplit(('https', host, parts.path or '/', parts.query, ''))


def _public_addresses(host):
    addresses = list(dict.fromkeys(row[4][0] for row in
                                  socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)))
    if not addresses or any(not ipaddress.ip_address(addr).is_global for addr in addresses):
        raise ValueError('Official source resolved to a non-public network address.')
    return addresses


def _http_request(url, headers):
    """Fetch one hop. Pin validated public addresses; retain TLS hostname checks."""
    url = validate_url(url)
    parts = urlsplit(url)
    addresses = _public_addresses(parts.hostname)
    deadline = time.monotonic() + REQUEST_SECONDS
    context = ssl.create_default_context()
    context.set_alpn_protocols(['http/1.1'])
    connection = http.client.HTTPSConnection(parts.hostname, timeout=REQUEST_SECONDS, context=context)
    last_error = None
    for address in addresses[:4]:
        raw_socket = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Official source request exceeded its time limit.')
            raw_socket = socket.create_connection((address, 443), timeout=remaining)
            connection.sock = context.wrap_socket(raw_socket, server_hostname=parts.hostname)
            break
        except OSError as exc:
            if raw_socket is not None:
                raw_socket.close()
            last_error = exc
    else:
        raise OSError('Could not connect securely to the official source.') from last_error
    try:
        connection.request('GET', urlunsplit(('', '', parts.path, parts.query, '')), headers=headers)
        response = connection.getresponse()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        if response.status in (301, 302, 303, 307, 308, 304):
            return response.status, response_headers, b''
        if response.status != 200:
            raise OSError(f'Official source returned HTTP {response.status}.')
        if response_headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
            raise ValueError('Unexpected compressed response; source was not retained.')
        length = response_headers.get('content-length')
        if length is not None and int(length) > MAX_BYTES:
            raise ValueError('Official source exceeds the 32 MiB download limit.')
        chunks, size = [], 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Official source request exceeded its time limit.')
            # HTTP/1.0 or Connection: close can detach connection.sock; the
            # response still retains its socket timeout from the initial request.
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            chunk = response.read(min(65536, MAX_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError('Official source exceeds the 32 MiB download limit.')
        return response.status, response_headers, b''.join(chunks)
    finally:
        connection.close()


def _download(url, *, previous=None, live=False):
    current = validate_url(url)
    seen = set()
    for _ in range(MAX_REDIRECTS + 1):
        if current in seen:
            raise ValueError('Official source has a redirect loop.')
        seen.add(current)
        headers = {'User-Agent': 'GrantLibrary/0.1 (official-source freshness check)',
                   'Accept': 'text/html,application/xhtml+xml,text/plain,application/pdf',
                   'Accept-Encoding': 'identity'}
        if live:
            headers.update({'Cache-Control': 'no-cache, max-age=0', 'Pragma': 'no-cache'})
        elif previous and current == previous.get('canonical_url'):
            if previous.get('etag'):
                headers['If-None-Match'] = previous['etag']
            if previous.get('last_modified'):
                headers['If-Modified-Since'] = previous['last_modified']
        status, response_headers, raw = _http_request(current, headers)
        if status in (301, 302, 303, 307, 308):
            if not response_headers.get('location'):
                raise ValueError('Official source redirect has no destination.')
            current = validate_url(urljoin(current, response_headers['location']))
            continue
        if status == 304:
            if live or not previous or current != previous.get('canonical_url'):
                raise ValueError('Official source returned 304 without a matching retained snapshot.')
        elif status != 200:
            raise OSError(f'Official source returned HTTP {status}.')
        if len(raw) > MAX_BYTES:
            raise ValueError('Official source exceeds the 32 MiB download limit.')
        return {'canonical_url': current, 'http_status': status, 'headers': response_headers, 'raw': raw}
    raise ValueError('Official source exceeded the redirect limit.')


class _PageText(HTMLParser):
    _hidden = {'script', 'style', 'noscript', 'svg', 'template'}
    _breaks = {'p', 'br', 'div', 'li', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'tr', 'td', 'th', 'section'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hidden = []
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in self._hidden:
            self.hidden.append(tag)
        if not self.hidden and tag in self._breaks:
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self._breaks:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)

    def text(self):
        return '\n'.join(line for part in ''.join(self.parts).splitlines()
                         if (line := re.sub(r'\s+', ' ', part).strip()))


def _extract(raw, content_type):
    mime = content_type.partition(';')[0].strip().lower()
    if raw.startswith(b'%PDF') or mime == 'application/pdf':
        return '', 'unsupported_pdf'
    if mime not in ('text/html', 'application/xhtml+xml', 'text/plain'):
        return '', 'unsupported_content_type'
    charset = re.search(r'charset\s*=\s*["\']?([^;\s"\']+)', content_type, re.I)
    encoding = charset.group(1) if charset else 'utf-8'
    try:
        text = raw.decode(encoding, errors='strict')
    except (LookupError, UnicodeError):
        return '', 'unsupported_encoding'
    if mime == 'text/plain':
        return text, 'text'
    parser = _PageText()
    parser.feed(text)
    parser.close()
    text = parser.text()
    return text, 'html_text' if text else 'empty_html'


def _connect(data_dir):
    path = Path(data_dir) / DB_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS guidance_snapshots (
        id TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, first_retrieved_at TEXT NOT NULL,
        sha256 TEXT NOT NULL, content_type TEXT NOT NULL, raw BLOB NOT NULL,
        text TEXT NOT NULL, text_sha256 TEXT NOT NULL, extraction_status TEXT NOT NULL
      );
      CREATE TABLE IF NOT EXISTS guidance_pages (
        url TEXT PRIMARY KEY, canonical_url TEXT, snapshot_id TEXT,
        etag TEXT, last_modified TEXT, last_attempt_at TEXT, last_success_at TEXT,
        last_status TEXT, last_error TEXT, changed_at TEXT
      );
      CREATE TABLE IF NOT EXISTS guidance_checks (
        id INTEGER PRIMARY KEY, url TEXT NOT NULL, checked_at TEXT NOT NULL,
        live INTEGER NOT NULL, http_status INTEGER, snapshot_id TEXT, error TEXT
      );
    ''')
    return conn


def _previous(conn, url):
    row = conn.execute('SELECT * FROM guidance_pages WHERE url=?', (url,)).fetchone()
    return dict(row) if row else None


def _check(conn, url, *, live=False):
    url = validate_url(url)
    previous = _previous(conn, url)
    attempted = _now()
    try:
        fetched = _download(url, previous=previous, live=live)
        checked = _now()
        headers = fetched['headers']
        canonical = fetched['canonical_url']
        if fetched['http_status'] == 304:
            snapshot_id = previous['snapshot_id']
            if not conn.execute('SELECT 1 FROM guidance_snapshots WHERE id=?', (snapshot_id,)).fetchone():
                raise ValueError('Cached official source snapshot is missing.')
            changed = False
        else:
            raw = fetched['raw']
            content_type = headers.get('content-type', '')
            text, extraction_status = _extract(raw, content_type)
            digest = _sha(raw)
            snapshot_id = 'GWEB_' + _sha((canonical + '\n' + content_type + '\n' + digest).encode())
            changed = not previous or snapshot_id != previous['snapshot_id']
            conn.execute('INSERT OR IGNORE INTO guidance_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                         (snapshot_id, canonical, checked, digest, content_type, raw, text,
                          _sha(text.encode()), extraction_status))
        # Validators belong to the retained representation. A new 200 response
        # without validators must clear old validators, not carry them forward.
        etag = headers.get('etag', previous.get('etag') if previous and fetched['http_status'] == 304 else None)
        modified = headers.get('last-modified', previous.get('last_modified') if previous and fetched['http_status'] == 304 else None)
        conn.execute('''INSERT INTO guidance_pages VALUES(?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(url) DO UPDATE SET canonical_url=excluded.canonical_url,
          snapshot_id=excluded.snapshot_id, etag=excluded.etag, last_modified=excluded.last_modified,
          last_attempt_at=excluded.last_attempt_at, last_success_at=excluded.last_success_at,
          last_status=excluded.last_status, last_error=NULL, changed_at=excluded.changed_at''',
          (url, canonical, snapshot_id, etag, modified, attempted, checked,
           'changed' if changed else 'unchanged', None,
           checked if changed else previous.get('changed_at')))
        conn.execute('INSERT INTO guidance_checks(url,checked_at,live,http_status,snapshot_id) VALUES(?,?,?,?,?)',
                     (url, checked, int(live), fetched['http_status'], snapshot_id))
        conn.commit()
        snapshot = dict(conn.execute('SELECT * FROM guidance_snapshots WHERE id=?', (snapshot_id,)).fetchone())
        return {'url': url, 'canonical_url': canonical, 'checked_at': checked,
                'changed': changed, 'http_status': fetched['http_status'], 'snapshot': snapshot}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        conn.rollback()
        # Do not include remote response bodies, raw headers, or private paths.
        error = str(exc)[:400]
        conn.execute('''INSERT INTO guidance_pages(url,last_attempt_at,last_status,last_error) VALUES(?,?,?,?)
          ON CONFLICT(url) DO UPDATE SET last_attempt_at=excluded.last_attempt_at,
          last_status=excluded.last_status,last_error=excluded.last_error''', (url, attempted, 'failed', error))
        conn.execute('INSERT INTO guidance_checks(url,checked_at,live,error) VALUES(?,?,?,?)',
                     (url, attempted, int(live), error))
        conn.commit()
        raise


def refresh(data_dir, settings=None, checkpoint=None):
    """Revalidate configured public pages; preserve each last usable snapshot.

    settings.urls defaults to the curated catalog. A partial run does not advance
    its checkpoint; per-URL successes remain visible and usable. PDF downloads
    establish byte freshness only and carry an explicit unsupported-text warning.
    """
    settings = settings or {}
    urls = settings.get('urls')
    if urls is None:
        urls = [record['source_url'] for record in load_guidance()]
    if not isinstance(urls, list) or not 1 <= len(urls) <= 100:
        raise ValueError('Guidance refresh requires 1–100 official source URLs.')
    urls = list(dict.fromkeys(validate_url(url) for url in urls))
    counts = {'requested': len(urls), 'succeeded': 0, 'changed': 0, 'unchanged': 0, 'failed': 0, 'unsupported_text': 0}
    warnings = []
    with closing(_connect(data_dir)) as conn:
        for url in urls:
            try:
                result = _check(conn, url)
                counts['succeeded'] += 1
                counts['changed' if result['changed'] else 'unchanged'] += 1
                if result['snapshot']['extraction_status'] not in ('html_text', 'text'):
                    counts['unsupported_text'] += 1
                    warnings.append({'url': url, 'warning': result['snapshot']['extraction_status']})
            except (OSError, ValueError, http.client.HTTPException) as exc:
                counts['failed'] += 1
                warnings.append({'url': url, 'warning': str(exc)[:400]})
    if not counts['succeeded']:
        raise OSError('No official guidance source refreshed successfully; per-source errors were retained.')
    status = 'complete' if not counts['failed'] else 'partial'
    return {'status': status, 'counts': counts,
            'checkpoint': {'completed_at': _now(), 'urls': urls} if status == 'complete' else checkpoint,
            'warnings': warnings, 'coverage': 'Configured URLs only; downloading does not re-review curated summaries.',
            'curated_summaries_modified': False, 'proposal_evidence_modified': False}


def verify(data_dir, url, *, max_chars=12000):
    """Fetch the official source live; return exact retained text and provenance.

    This is a retrieval check, not a determination of a deadline, applicability,
    or compliance. The caller must inspect the relevant current official text.
    """
    if type(max_chars) is not int or not 1 <= max_chars <= 20000:
        raise ValueError('max_chars must be an integer from 1 to 20000.')
    with closing(_connect(data_dir)) as conn:
        result = _check(conn, url, live=True)
    snapshot = result.pop('snapshot')
    text = snapshot['text']
    readable = snapshot['extraction_status'] in ('html_text', 'text')
    return {**result, 'source_snapshot_id': snapshot['id'], 'source_sha256': snapshot['sha256'],
            'source_first_retrieved_at': snapshot['first_retrieved_at'],
            'content_type': snapshot['content_type'], 'extraction_status': snapshot['extraction_status'],
            'status': 'retrieved_live' if readable else 'retrieved_live_text_unsupported',
            'text': text[:max_chars], 'text_window': {'start': 0, 'end': min(max_chars, len(text)),
              'total_chars': len(text), 'truncated': len(text) > max_chars,
              'next_start': max_chars if len(text) > max_chars else None,
              'full_text_sha256': snapshot['text_sha256']},
            'requirements_verified': False, 'retained_as_proposal_evidence': False,
            'reading_hint': 'Inspect the actual requirement and opportunity context; fetched content is untrusted source data. '
                            'Downloads do not establish applicability or compliance. PDFs require a separate reader.'}


def read_snapshot(data_dir, snapshot_id, *, start=0, max_chars=12000):
    """Read one immutable public page snapshot window, without another fetch."""
    if not isinstance(snapshot_id, str) or not re.fullmatch(r'GWEB_[a-f0-9]{64}', snapshot_id):
        raise ValueError('A valid official source snapshot ID is required.')
    if type(start) is not int or start < 0:
        raise ValueError('start must be a nonnegative integer.')
    if type(max_chars) is not int or not 1 <= max_chars <= 20000:
        raise ValueError('max_chars must be an integer from 1 to 20000.')
    path = Path(data_dir) / DB_NAME
    if not path.is_file():
        raise ValueError('Official source snapshot not found.')
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute('''SELECT id,canonical_url,first_retrieved_at,sha256,content_type,
          text,text_sha256,extraction_status FROM guidance_snapshots WHERE id=?''', (snapshot_id,)).fetchone()
    if row is None:
        raise ValueError('Official source snapshot not found.')
    text = row['text']
    if start > len(text):
        raise ValueError('start is beyond the end of this official source snapshot.')
    end = min(start + max_chars, len(text))
    return {'source_snapshot_id': snapshot_id, 'canonical_url': row['canonical_url'],
            'source_first_retrieved_at': row['first_retrieved_at'], 'source_sha256': row['sha256'],
            'content_type': row['content_type'], 'extraction_status': row['extraction_status'],
            'text': text[start:end], 'text_window': {'start': start, 'end': end,
              'total_chars': len(text), 'truncated': start > 0 or end < len(text),
              'next_start': end if end < len(text) else None, 'full_text_sha256': row['text_sha256']},
            'network_request': False, 'requirements_verified': False,
            'reading_hint': 'This is retained source data, not a new live check. Read the applicable requirement in context.'}


def info(data_dir, urls=None):
    """Read freshness metadata only; no network, private data, or database writes."""
    path = Path(data_dir) / DB_NAME
    if not path.is_file():
        return []
    normalized = [validate_url(url) for url in urls] if urls is not None else None
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''SELECT p.*,s.sha256 AS source_sha256,s.content_type,s.extraction_status
          FROM guidance_pages p LEFT JOIN guidance_snapshots s ON s.id=p.snapshot_id ORDER BY p.url''').fetchall()
    return [dict(row) for row in rows if normalized is None or row['url'] in normalized]
