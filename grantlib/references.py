"""Resolve one public identifier; metadata is not verification of a claim.

Authoritative formats:
https://www.crossref.org/documentation/retrieve-metadata/rest-api/
https://github.com/CrossRef/rest-api-doc/blob/master/api_format.md
https://www.ncbi.nlm.nih.gov/home/develop/api/
https://dtd.nlm.nih.gov/ncbi/pubmed/doc/out/250101/el-CommentsCorrections.html

No archive, local configuration, credentials, model or full-text service is read.
The injectable transport accepts (url, *, timeout, max_bytes) and returns Response.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
from html.parser import HTMLParser
import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import quote, urlencode, urlsplit
import xml.etree.ElementTree as ET


MAX_BYTES = 2 * 1024 * 1024
TIMEOUT = 12
_HOSTS = {'api.crossref.org', 'eutils.ncbi.nlm.nih.gov'}
_DOI = re.compile(r'10\.\d{4,9}/[^\s\\]+', re.ASCII)
_PMID = re.compile(r'[1-9][0-9]{0,9}', re.ASCII)
_BASE_LIMITATIONS = [
    'Identifier and bibliographic metadata resolution only; semantic support for a grant claim is not assessed.',
    'Full text, methods, results, and cited claims were not independently read or verified.',
    'Missing publication notices do not establish that a work has no correction, retraction, or expression of concern.',
]


class ReferenceError(RuntimeError):
    """Controlled lookup failure; messages never include response bodies."""
    code = 'reference_error'


class ReferenceInputError(ReferenceError, ValueError):
    code = 'invalid_identifier'


class ReferenceProtocolError(ReferenceError):
    code = 'invalid_response'


class ReferenceNetworkError(ReferenceError):
    code = 'network_error'


class ReferenceRateLimited(ReferenceError):
    code = 'rate_limited'

    def __init__(self, retry_after=1):
        self.retry_after = max(1, min(300, int(retry_after + .999)))
        super().__init__(f'Reference service rate limit; retry after {self.retry_after} seconds.')


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict
    body: bytes


def normalize_identifier(identifier):
    """Return {'kind': 'pmid'|'doi', 'value': canonical}; never accept a URL."""
    if not isinstance(identifier, str) or len(identifier) > 1024:
        raise ReferenceInputError('Supply one PMID or DOI, at most 1,024 characters.')
    value = identifier.strip()
    kind = None
    for prefix in ('pmid:', 'doi:'):
        if value.lower().startswith(prefix):
            kind, value = prefix[:-1], value[len(prefix):].strip()
            break
    if kind in (None, 'pmid') and _PMID.fullmatch(value):
        return {'kind': 'pmid', 'value': value}
    if (kind in (None, 'doi') and value.isascii()
            and all(32 < ord(c) < 127 for c in value) and _DOI.fullmatch(value)):
        return {'kind': 'doi', 'value': value.lower()}
    raise ReferenceInputError('Supply one bare PMID or DOI (optional PMID:/DOI: label), not a URL or citation text.')


class _Throttle:
    """Process-wide one-request/second limit; long cooldowns fail promptly."""
    def __init__(self, clock=time.monotonic, sleep=time.sleep):
        self.clock, self.sleep = clock, sleep
        self.next_allowed = 0.0
        self.lock = threading.Lock()

    def wait(self):
        with self.lock:
            delay = max(0, self.next_allowed - self.clock())
            if delay > 1.01:
                raise ReferenceRateLimited(delay)
            if delay:
                self.sleep(delay)
            self.next_allowed = self.clock() + 1

    def cooldown(self, seconds):
        with self.lock:
            self.next_allowed = max(self.next_allowed, self.clock() + seconds)


_THROTTLE = _Throttle()
_NETWORK_LOCK = threading.Lock()


def _retry_after(headers):
    value = headers.get('retry-after', '')
    try:
        seconds = float(value)
        if not 0 <= seconds <= 300:
            return 300
        return max(1, seconds)
    except (TypeError, ValueError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
            return max(1, min(300, seconds))
        except (TypeError, ValueError, OverflowError):
            return 60


def _request_official(url, *, timeout, max_bytes, hosts, user_agent):
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.hostname not in hosts or parts.port is not None
            or parts.username or parts.password or parts.fragment):
        raise ReferenceInputError('Only fixed official reference API hosts are allowed.')
    if not _NETWORK_LOCK.acquire(blocking=False):
        raise ReferenceRateLimited(1)
    connection = None
    watchdog = None
    try:
        _THROTTLE.wait()
        started = time.monotonic()
        connection = http.client.HTTPSConnection(parts.hostname, timeout=timeout)
        connection.connect()
        raw_socket = connection.sock
        remaining = max(.001, timeout - (time.monotonic() - started))
        raw_socket.settimeout(remaining)
        # A joined watchdog bounds slow headers/body trickles as well as idle reads.
        def expire():
            try:
                raw_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        watchdog = threading.Timer(remaining, expire)
        watchdog.daemon = True
        watchdog.start()
        path = parts.path + ('?' + parts.query if parts.query else '')
        connection.request('GET', path, headers={
            'User-Agent': user_agent,
            'Accept': 'application/json, application/xml, text/xml',
            'Accept-Encoding': 'identity',
        })
        response = connection.getresponse()
        headers = {k.lower(): v for k, v in response.getheaders()}
        if headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
            raise ReferenceProtocolError('Compressed reference responses are not accepted.')
        try:
            if int(headers.get('content-length', '0')) > max_bytes:
                raise ReferenceProtocolError('Reference response exceeds the size limit.')
        except ValueError:
            raise ReferenceProtocolError('Invalid response length.') from None
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ReferenceProtocolError('Reference response exceeds the size limit.')
        if response.status == 429:
            _THROTTLE.cooldown(_retry_after(headers))
        return Response(response.status, headers, body)
    except (OSError, http.client.HTTPException):
        raise ReferenceNetworkError('The public reference service could not be reached within the request limit.') from None
    finally:
        if watchdog is not None:
            watchdog.cancel()
            watchdog.join()
        if connection is not None:
            connection.close()
        _NETWORK_LOCK.release()


def _network(url, *, timeout, max_bytes):
    return _request_official(url, timeout=timeout, max_bytes=max_bytes, hosts=_HOSTS,
                            user_agent='GrantStudio-ReferenceResolver/1.0 (single-reference metadata)')


class _PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, value):
        self.parts.append(value)

    def handle_starttag(self, tag, _attrs):
        if tag.split(':')[-1] in ('p', 'title', 'sec', 'br'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        self.handle_starttag(tag, [])


def _plain(value):
    if not isinstance(value, str):
        return None
    parser = _PlainText()
    parser.feed(value)
    return '\n'.join(' '.join(line.split()) for line in ''.join(parser.parts).splitlines() if line.strip()) or None


def _text(node):
    return ' '.join(''.join(node.itertext()).split()) if node is not None else None


def _name_parts(family, given, suffix, literal):
    """Structured author name for citation styles; a collective name stays literal."""
    if literal:
        return {'literal': literal}
    parts = {'family': family or None, 'given': given or None}
    if suffix:
        parts['suffix'] = suffix
    return parts


def _notices(scope):
    return {'retracted': None, 'has_correction': None, 'expression_of_concern': None,
            'is_retraction_notice': None, 'is_correction_notice': None,
            'is_expression_of_concern_notice': None, 'relations': [], 'scope': scope,
            'complete': False, 'unknowns': ['Unreported, newly issued, or unindexed notices may be absent.']}


def _snapshot(requested, provider, url, body):
    return {'status': 'not_found', 'requested': requested,
            'identifiers': {'pmid': None, 'doi': None}, 'observed_identifiers': [],
            'title': None, 'authors': [], 'author_parts': [], 'journal': None, 'journal_abbreviation': None,
            'year': None, 'volume': None, 'issue': None, 'pages': None, 'work_type': None, 'abstract': None,
            'publication_notices': _notices('No matching metadata record was accepted.'),
            'provider': provider, 'source_url': None, 'metadata_url': url,
            'retrieved_at': datetime.now(timezone.utc).isoformat(),
            'response_sha256': hashlib.sha256(body).hexdigest(),
            'verification': {'metadata_only': True, 'semantic_support': False, 'full_text': False},
            'limitations': list(_BASE_LIMITATIONS)}


def _known_id(value, kind):
    try:
        result = normalize_identifier(value)
        return result['value'] if result['kind'] == kind else None
    except ReferenceInputError:
        return None


def _crossref(data, result):
    if not isinstance(data, dict) or data.get('status') != 'ok' or not isinstance(data.get('message'), dict):
        raise ReferenceProtocolError('Crossref did not return a singleton metadata record.')
    work = data['message']
    doi = _known_id(work.get('DOI'), 'doi')
    if not doi:
        raise ReferenceProtocolError('Crossref returned a record without a valid DOI.')
    result['observed_identifiers'] = [{'doi': doi}]
    if doi != result['requested']['value']:
        result['status'] = 'mismatch'
        result['limitations'].append('The returned DOI did not match the requested DOI; its metadata was not accepted.')
        return
    result.update(status='resolved', identifiers={'pmid': None, 'doi': doi},
                  source_url='https://doi.org/' + quote(doi, safe='/'))
    def first(field):
        values = work.get(field, [])
        return _plain(values[0]) if isinstance(values, list) and values else None
    result['title'], result['journal'] = first('title'), first('container-title')
    authors = work.get('author', [])
    if not isinstance(authors, list):
        raise ReferenceProtocolError('Invalid Crossref author metadata.')
    result['authors'], result['author_parts'] = [], []
    for a in authors[:1000]:
        if not isinstance(a, dict):
            continue
        name = (_plain(' '.join(str(a.get(k) or '') for k in ('given', 'family'))) or _plain(a.get('name')))
        if not name:
            continue
        result['authors'].append(name)
        result['author_parts'].append(_name_parts(_plain(a.get('family')), _plain(a.get('given')), None,
                                                  _plain(a.get('name')) if not a.get('family') else None))
    result['journal_abbreviation'] = first('short-container-title')
    result['volume'] = _plain(work.get('volume')) if isinstance(work.get('volume'), str) else None
    result['issue'] = _plain(work.get('issue')) if isinstance(work.get('issue'), str) else None
    result['pages'] = _plain(work.get('page')) if isinstance(work.get('page'), str) else None
    result['work_type'] = _plain(work.get('type')) if isinstance(work.get('type'), str) else None
    if len(authors) > 1000:
        result['limitations'].append('Author list truncated after 1,000 entries.')
    for field in ('issued', 'published', 'published-print', 'published-online'):
        dates = work.get(field, {})
        parts = dates.get('date-parts', []) if isinstance(dates, dict) else []
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            year = parts[0][0]
            if type(year) is int and 1000 <= year <= 9999:
                result['year'] = year
                break
    result['abstract'] = _plain(work.get('abstract'))
    notices = _notices('Crossref update-to and relation fields in this exact DOI record only; no reverse-update search.')
    updates = work.get('update-to', [])
    if not isinstance(updates, list) or not isinstance(work.get('relation', {}), dict):
        raise ReferenceProtocolError('Invalid Crossref update metadata.')
    for update in updates[:100]:
        if not isinstance(update, dict):
            raise ReferenceProtocolError('Invalid Crossref update metadata.')
        kind, target = _plain(update.get('type')), _known_id(update.get('DOI'), 'doi')
        notices['relations'].append({'type': kind, 'direction': 'this_record_updates', 'doi': target})
        # update-to identifies the work changed by this record, not a notice
        # changing this record. Never label a retraction notice as retracted.
        if target and target != doi:
            flag = {'retraction': 'is_retraction_notice', 'correction': 'is_correction_notice',
                    'expression-of-concern': 'is_expression_of_concern_notice'}.get((kind or '').lower())
            if flag:
                notices[flag] = True
    for kind, relations in work.get('relation', {}).items():
        if not isinstance(relations, list):
            raise ReferenceProtocolError('Invalid Crossref relation metadata.')
        for relation in relations[:100]:
            if isinstance(relation, dict):
                notices['relations'].append({'type': str(kind), 'direction': 'as_reported_by_provider',
                    'doi': _known_id(relation.get('id'), 'doi') if relation.get('id-type') == 'doi' else None})
    notices['unknowns'].append('Whether this work has been retracted or corrected is unknown unless explicitly reported; related notices were not fetched.')
    if len(updates) > 100 or len(notices['relations']) > 100:
        notices['relations'] = notices['relations'][:100]
        result['limitations'].append('Publication relations truncated after 100 entries.')
    result['publication_notices'] = notices
    result['limitations'].append('Crossref coverage depends on registered/deposited metadata; a missing DOI is not proof it is invalid or absent from another registration agency.')
    result['limitations'].append('DOI lookups do not cross-map to PubMed; an absent PMID here does not establish that the work is unindexed.')
    if not result['abstract']:
        result['limitations'].append('No abstract was supplied by Crossref for this record.')


def _pubmed(body, result):
    upper = body.upper()
    if b'<!ENTITY' in upper or re.search(br'<!DOCTYPE[^>]*\[', upper):
        raise ReferenceProtocolError('XML entity declarations and internal DTD subsets are not accepted.')
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError, RecursionError):
        raise ReferenceProtocolError('PubMed returned invalid XML.') from None
    stack, count = [(root, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > 50000 or depth > 100:
            raise ReferenceProtocolError('PubMed XML exceeds structural limits.')
        stack.extend((child, depth + 1) for child in node)
    if root.tag != 'PubmedArticleSet' or root.find('.//ERROR') is not None:
        raise ReferenceProtocolError('PubMed returned an error or unexpected document.')
    if any(child.tag not in ('PubmedArticle', 'PubmedBookArticle') for child in root):
        raise ReferenceProtocolError('PubMed returned unexpected record elements.')
    articles = root.findall('PubmedArticle') + root.findall('PubmedBookArticle')
    def primary(item):
        return item.find('MedlineCitation') if item.tag == 'PubmedArticle' else item.find('BookDocument')
    ids = [_known_id(_text(primary(a).find('PMID')) if primary(a) is not None else None, 'pmid') for a in articles]
    result['observed_identifiers'] = [{'pmid': pmid} for pmid in ids[:10]]
    if not articles:
        result['limitations'].append('PubMed returned no indexed record for this PMID.')
        return
    if len(articles) > 1:
        result['status'] = 'ambiguous'
        result['limitations'].append('PubMed returned multiple records for one PMID; none was selected.')
        return
    if not ids[0]:
        raise ReferenceProtocolError('PubMed returned a record without a valid primary PMID.')
    if ids[0] != result['requested']['value']:
        result['status'] = 'mismatch'
        result['limitations'].append('The returned primary PMID did not match; its metadata was not accepted.')
        return
    item, citation = articles[0], primary(articles[0])
    book = item.tag == 'PubmedBookArticle'
    article = citation if book else citation.find('Article')
    if article is None:
        raise ReferenceProtocolError('PubMed returned no article metadata.')
    result.update(status='resolved', identifiers={'pmid': ids[0], 'doi': None},
                  source_url=f'https://pubmed.ncbi.nlm.nih.gov/{ids[0]}/')
    result['title'] = _text(article.find('ArticleTitle'))
    result['journal'] = _text(article.find('Journal/Title'))
    if book:
        result['book_title'] = _text(article.find('Book/BookTitle'))
    authors = article.findall('AuthorList/Author')
    result['authors'], result['author_parts'] = [], []
    for author in authors[:1000]:
        collective = _text(author.find('CollectiveName'))
        name = collective or ' '.join(filter(None, [_text(author.find(k)) for k in ('ForeName', 'LastName', 'Suffix')]))
        if not name:
            continue
        result['authors'].append(name)
        result['author_parts'].append(_name_parts(
            _text(author.find('LastName')), _text(author.find('ForeName')) or _text(author.find('Initials')),
            _text(author.find('Suffix')), collective))
    result['journal_abbreviation'] = _text(article.find('Journal/ISOAbbreviation')) or _text(citation.find('MedlineJournalInfo/MedlineTA'))
    result['volume'] = _text(article.find('Journal/JournalIssue/Volume'))
    result['issue'] = _text(article.find('Journal/JournalIssue/Issue'))
    pages = _text(article.find('Pagination/MedlinePgn'))
    if not pages:
        start, end = _text(article.find('Pagination/StartPage')), _text(article.find('Pagination/EndPage'))
        pages = start + ('-' + end if end and end != start else '') if start else None
    if not pages:
        pages = next((_text(n) for n in article.findall('ELocationID') if n.get('EIdType') == 'pii' and _text(n)), None)
    result['pages'] = pages
    result['work_type'] = 'book' if book else 'journal-article'
    if len(authors) > 1000:
        result['limitations'].append('Author list truncated after 1,000 entries.')
    date = article.find('Book/PubDate' if book else 'Journal/JournalIssue/PubDate')
    date_text = (_text(date.find('Year')) or _text(date.find('MedlineDate'))) if date is not None else None
    match = re.search(r'\b([12][0-9]{3})\b', date_text or '')
    result['year'] = int(match[1]) if match else None
    sections = []
    for section in article.findall('Abstract/AbstractText'):
        text = _text(section)
        if text:
            sections.append((section.get('Label') + ': ' if section.get('Label') else '') + text)
    result['abstract'] = '\n\n'.join(sections) or None
    data_name = 'PubmedBookData' if book else 'PubmedData'
    dois = {_known_id(_text(n), 'doi') for n in item.findall(f'{data_name}/ArticleIdList/ArticleId')
            if n.get('IdType') == 'doi'} - {None}
    if len(dois) == 1:
        result['identifiers']['doi'] = next(iter(dois))
    elif len(dois) > 1:
        result['limitations'].append('PubMed reports multiple distinct DOIs; no canonical DOI was selected.')
        result['reported_dois'] = sorted(dois)
    notices = _notices('Publication types and directional CommentsCorrections links in this PubMed record only.')
    mapping = {'RetractionIn': 'retracted', 'RetractionOf': 'is_retraction_notice',
               'ErratumIn': 'has_correction', 'ErratumFor': 'is_correction_notice',
               'CorrectedandRepublishedIn': 'has_correction',
               'ExpressionOfConcernIn': 'expression_of_concern',
               'ExpressionOfConcernFor': 'is_expression_of_concern_notice'}
    relations = citation.findall('CommentsCorrectionsList/CommentsCorrections')
    if len(relations) > 100:
        result['limitations'].append('Publication relations truncated after 100 entries.')
    for relation in relations[:100]:
        kind = relation.get('RefType')
        notices['relations'].append({'type': kind, 'pmid': _known_id(_text(relation.find('PMID')), 'pmid'),
                                    'description': _text(relation.find('RefSource'))})
        if kind in mapping:
            notices[mapping[kind]] = True
    types = [_text(n) for n in article.findall('PublicationTypeList/PublicationType')]
    for name, flag in {'Retracted Publication': 'retracted', 'Retraction of Publication': 'is_retraction_notice',
                       'Published Erratum': 'is_correction_notice'}.items():
        if name in types:
            notices[flag] = True
    result['publication_types'] = types
    result['publication_notices'] = notices
    result['limitations'].append('PubMed indexing and linked publication notices may lag publisher updates; linked notices were not fetched.')
    if not result['abstract']:
        result['limitations'].append('No abstract was supplied by PubMed for this record.')


def resolve(identifier, *, transport=None):
    """Resolve one PMID/DOI; return a JSON-safe metadata snapshot.

    status is resolved, not_found, ambiguous, or mismatch. Transport/protocol/input
    failures raise ReferenceError subclasses. No redirects, retries, crawling,
    arbitrary URL fetches or semantic/full-text verification occur.
    """
    requested = normalize_identifier(identifier)
    provider = 'pubmed' if requested['kind'] == 'pmid' else 'crossref'
    if provider == 'pubmed':
        url = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?' + urlencode({
            'db': 'pubmed', 'id': requested['value'], 'retmode': 'xml', 'tool': 'grantstudio_reference_resolver'})
    else:
        url = 'https://api.crossref.org/works/' + quote(requested['value'], safe='')
    try:
        response = (transport or _network)(url, timeout=TIMEOUT, max_bytes=MAX_BYTES)
    except (OSError, TimeoutError):
        raise ReferenceNetworkError('The public reference service could not be reached within the request limit.') from None
    if (not isinstance(response, Response) or type(response.status) is not int
            or not isinstance(response.headers, dict) or not isinstance(response.body, bytes)):
        raise ReferenceProtocolError('The reference transport returned an invalid response.')
    if len(response.body) > MAX_BYTES:
        raise ReferenceProtocolError('Reference response exceeds the size limit.')
    headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
    if 300 <= response.status < 400:
        raise ReferenceProtocolError('Reference API redirects are refused.')
    if response.status == 429:
        raise ReferenceRateLimited(_retry_after(headers))
    if response.status not in (200, 404):
        raise ReferenceNetworkError(f'The public reference service returned HTTP {response.status}.')
    result = _snapshot(requested, provider, url, response.body)
    if response.status == 404:
        result['limitations'].append(f'No record was returned by {provider}; this does not establish absence from other bibliographic registries.')
        return result
    if provider == 'pubmed':
        _pubmed(response.body, result)
    else:
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeError, RecursionError):
            raise ReferenceProtocolError('Crossref returned invalid JSON.') from None
        _crossref(data, result)
    return result
