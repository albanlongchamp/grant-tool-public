"""Read one explicitly identified PMC OA article, never proposal text.

Provider contracts checked 2026-10-31:
https://pmc.ncbi.nlm.nih.gov/tools/id-converter-api/
https://pmc-oa-opendata.s3.amazonaws.com/README.txt
https://pmc.ncbi.nlm.nih.gov/about/copyright/

The selected provider version is frozen by hashes and exact XML bytes. Reading
or retaining an extraction does not verify a claim, the whole article, or its
current status. The dataset can update objects under the same version prefix.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit
import xml.etree.ElementTree as ET

from . import references


MAX_XML_BYTES = 5 * 1024 * 1024
MAX_METADATA_BYTES = 128 * 1024
MAX_TEXT_CHARS = 500000
MAX_PARAGRAPHS = 2000
MAX_XML_NODES = 100000
MAX_XML_DEPTH = 100
TOTAL_TIMEOUT = 45
REQUEST_TIMEOUT = 15
EXTRACTOR_VERSION = 'jats-paragraphs-v1'
_PMC = re.compile(r'PMC[1-9][0-9]{0,9}(?:\.[1-9][0-9]{0,4})?', re.ASCII)
_CLOUD = 'https://pmc-oa-opendata.s3.amazonaws.com'
_IDENTITY = 'https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/'
_HOSTS = {'pmc.ncbi.nlm.nih.gov', 'pmc-oa-opendata.s3.amazonaws.com'}
_LICENSES = {'CC BY', 'CC BY-SA', 'CC BY-NC', 'CC BY-NC-SA', 'CC BY-ND', 'CC BY-NC-ND', 'CC0'}


class PublicationTextError(references.ReferenceError):
    code = 'full_text_error'


class PublicationTextInputError(PublicationTextError, ValueError):
    code = 'invalid_pmcid'


class PublicationTextUnavailable(PublicationTextError):
    code = 'full_text_unavailable'


class PublicationTextProtocolError(PublicationTextError):
    code = 'invalid_full_text'


def normalize_pmcid(value):
    if not isinstance(value, str) or not _PMC.fullmatch(value.strip().upper()):
        raise PublicationTextInputError('Enter one PMCID, such as PMC1234567 or PMC1234567.1, not a URL or PMID.')
    return value.strip().upper()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _network(url, *, timeout, max_bytes):
    return references._request_official(url, timeout=timeout, max_bytes=max_bytes, hosts=_HOSTS,
        user_agent='GrantStudio-PublicationReader/1.0 (single explicit PMCID)')


def _json(body):
    try:
        value = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise PublicationTextProtocolError('The article service returned invalid JSON.') from None
    if not isinstance(value, dict):
        raise PublicationTextProtocolError('The article service returned an unexpected metadata record.')
    return value


def _bool(value):
    return value if type(value) is bool else None


def _pmid(value):
    if value is None or value == '':
        return None
    if type(value) not in (str, int) or not re.fullmatch(r'[1-9][0-9]{0,9}', str(value)):
        raise PublicationTextProtocolError('Invalid PMID in article metadata.')
    return str(value)


def _doi(value):
    if not value:
        return None
    try:
        result = references.normalize_identifier(value)
    except references.ReferenceError:
        raise PublicationTextProtocolError('Invalid DOI in article metadata.') from None
    if result['kind'] != 'doi':
        raise PublicationTextProtocolError('Invalid DOI in article metadata.')
    return result['value']


def _local(node):
    return node.tag.rsplit('}', 1)[-1] if isinstance(node.tag, str) else ''


def _children(node, name):
    return [child for child in node if _local(child) == name] if node is not None else []


def _child(node, name):
    return next(iter(_children(node, name)), None)


def _text(node):
    return ' '.join(''.join(node.itertext()).split()) if node is not None else ''


def _xml(body):
    # Reject wide encodings before the byte-level entity/DTD preflight. Without
    # this, UTF-16/32 NUL padding can hide entity declarations from those checks.
    if b'\x00' in body:
        raise PublicationTextProtocolError('Wide-encoded XML is not supported by the bounded article reader.')
    upper = body.upper()
    if b'<!ENTITY' in upper or re.search(br'<!DOCTYPE[^>]*\[', upper):
        raise PublicationTextProtocolError('XML entities and internal DTD subsets are not accepted.')
    try:
        root = ET.fromstring(body)
    except (ET.ParseError, ValueError, RecursionError):
        raise PublicationTextProtocolError('The article XML could not be parsed safely.') from None
    stack, count = [(root, 0)], 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if count > MAX_XML_NODES or depth > MAX_XML_DEPTH:
            raise PublicationTextProtocolError('Article XML exceeds structural limits.')
        stack.extend((child, depth + 1) for child in node)
    if _local(root) != 'article':
        raise PublicationTextProtocolError('Expected a single JATS article.')
    return root


def _identity(data, requested):
    records = data.get('records')
    if data.get('status') != 'ok' or not isinstance(records, list) or len(records) != 1:
        raise PublicationTextUnavailable('PMC did not resolve one available article for this identifier.')
    record = records[0]
    base = requested.split('.')[0]
    if not isinstance(record, dict) or record.get('pmcid') != base:
        raise PublicationTextProtocolError('PMC returned a different article identity.')
    echoed = record.get('requested-id', requested)
    # The current converter echoes the base PMCID even for explicit versions.
    # Exact version membership is validated separately below.
    if not isinstance(echoed, str) or echoed.upper() not in {requested, base}:
        raise PublicationTextProtocolError('PMC did not echo the requested article identifier.')
    if record.get('live') is False or record.get('live') == 'false':
        raise PublicationTextUnavailable('This PMC article is not currently available in full text.')
    versions = record.get('versions')
    if not isinstance(versions, list) or not versions or not all(isinstance(v, dict) for v in versions):
        raise PublicationTextProtocolError('PMC did not supply version information; no version was guessed.')
    explicit = '.' in requested
    matches = [v for v in versions if v.get('pmcid') == requested] if explicit else [v for v in versions if v.get('current') is True]
    if len(matches) != 1:
        raise PublicationTextProtocolError('PMC did not identify one requested/current article version; no version was guessed.')
    selected = matches[0]
    version = selected.get('pmcid', '')
    if not isinstance(version, str) or not _PMC.fullmatch(version) or '.' not in version or version.split('.')[0] != base:
        raise PublicationTextProtocolError('PMC returned an invalid versioned article identifier.')
    if selected.get('live') is False or selected.get('live') == 'false':
        raise PublicationTextUnavailable('The requested article version is not currently available.')
    return record, selected, 'explicit' if explicit else 'pmc_current'


def _check_xml_identity(root, canonical, version, aiid):
    article_meta = _child(_child(root, 'front'), 'article-meta')
    if article_meta is None:
        raise PublicationTextProtocolError('Article identity metadata is missing from the XML.')
    observed = {}
    for node in _children(article_meta, 'article-id'):
        observed.setdefault(node.get('pub-id-type'), set()).add(_text(node))
    pmcids = observed.get('pmcid', set()) | observed.get('pmc', set())
    normalized = {value if value.startswith('PMC') else 'PMC' + value for value in pmcids}
    if normalized != {canonical['pmcid']}:
        raise PublicationTextProtocolError('The XML PMCID does not match the requested article.')
    if observed.get('pmcid-ver') and observed['pmcid-ver'] != {version}:
        raise PublicationTextProtocolError('The XML article version does not match its provider object.')
    if observed.get('doi'):
        dois = {_doi(value) for value in observed['doi']}
        if len(dois) != 1 or (canonical.get('doi') and dois != {canonical['doi']}):
            raise PublicationTextProtocolError('The XML DOI conflicts with the article identity.')
        canonical['doi'] = next(iter(dois))
    if observed.get('pmid'):
        pmids = {_pmid(value) for value in observed['pmid']}
        if len(pmids) != 1 or (canonical.get('pmid') and pmids != {canonical['pmid']}):
            raise PublicationTextProtocolError('The XML PMID conflicts with the article identity.')
        canonical['pmid'] = next(iter(pmids))
    if aiid is not None and observed.get('pmcaiid') and observed['pmcaiid'] != {str(aiid)}:
        raise PublicationTextProtocolError('The XML article-instance identity conflicts with PMC.')
    return article_meta


def _license(article_meta, code):
    permissions = _child(article_meta, 'permissions')
    licenses = _children(permissions, 'license')
    urls, statements = [], []
    for node in licenses:
        url = node.get('{http://www.w3.org/1999/xlink}href') or node.get('href') or ''
        if url:
            parts = urlsplit(url)
            if parts.scheme not in ('http', 'https') or parts.username or parts.password:
                raise PublicationTextProtocolError('The XML license has an invalid URL.')
            if parts.hostname in ('creativecommons.org', 'www.creativecommons.org'):
                match = re.match(r'/licenses/([a-z-]+)/', parts.path)
                observed = 'CC ' + match[1].upper() if match else 'CC0' if parts.path.startswith('/publicdomain/zero/') else None
                if observed and observed != code:
                    raise PublicationTextProtocolError('Provider and XML license terms conflict.')
            urls.append(url)
        statement = _text(node)
        if statement:
            statements.append(statement)
    statement = '\n\n'.join(statements)
    if len(statement) > 16000 or any(len(url) > 2000 for url in urls):
        raise PublicationTextProtocolError('Article license metadata exceeds its limit.')
    return {'code': code, 'open_access': True, 'statement': statement,
            'url': urls[0] if urls else '', 'reported_urls': list(dict.fromkeys(urls)),
            'source': 'metadata_and_xml' if statement or urls else 'metadata_only'}


def _passage_text(node):
    """Keep sub/superscript meaning visible in the plain-text extraction."""
    def inline(element):
        content = element.text or ''
        for child in element:
            value = inline(child)
            name = _local(child)
            if name == 'sub':
                value = '_{' + value + '}'
            elif name == 'sup':
                value = '^{' + value + '}'
            elif name in ('p', 'list-item', 'break'):
                value = '\n' + value + '\n'
            content += value + (child.tail or '')
        return content
    return '\n'.join(' '.join(line.split()) for line in inline(node).splitlines() if line.strip())


def _extract(root):
    paths = {}
    formula_tags = {'inline-formula', 'disp-formula', 'math', 'tex-math'}
    formula_count = 0
    def index(node, path, in_formula=False):
        nonlocal formula_count
        paths[id(node)] = path
        is_formula = _local(node) in formula_tags
        if is_formula and not in_formula:
            formula_count += 1
        counts = {}
        for child in node:
            name = _local(child)
            counts[name] = counts.get(name, 0) + 1
            index(child, path + '/' + name + '[' + str(counts[name]) + ']', in_formula or is_formula)
    index(root, '/article[1]')
    tags = [_local(n) for n in root.iter()]
    omitted = {'figures': tags.count('fig'), 'tables': tags.count('table-wrap'),
        'supplements': tags.count('supplementary-material'),
        'formulas': formula_count,
        'reference_entries': tags.count('ref'), 'paragraphs_with_unsupported_content': 0}
    paragraphs, total = [], 0
    forbidden = formula_tags | {'graphic', 'inline-graphic', 'media', 'table-wrap', 'table', 'fig',
        'supplementary-material', 'alternatives', 'fn', 'fn-group', 'ref-list', 'caption'}
    excluded_blocks = forbidden | {'app-group'}
    def walk(node, section, kind):
        nonlocal total
        name = _local(node)
        if name in excluded_blocks:
            return
        if name == 'sec':
            title = _text(_child(node, 'title')) or 'Untitled section'
            section = section + [title]
        if name == 'p':
            if any(_local(n) in forbidden for n in node.iter()):
                omitted['paragraphs_with_unsupported_content'] += 1
                return
            text = _passage_text(node)
            if not text:
                return
            total += len(text)
            if total > MAX_TEXT_CHARS or len(paragraphs) >= MAX_PARAGRAPHS:
                raise PublicationTextUnavailable('This article exceeds the bounded reader text limit; no partial article was retained.')
            number = len(paragraphs) + 1
            heading = ' / '.join(section) or ('Abstract' if kind == 'abstract' else 'Article body')
            path = paths[id(node)]
            paragraphs.append({'id': f'p{number:04d}', 'section': heading, 'kind': kind,
                'locator': f'{heading}; paragraph {number}; XML {path}', 'xml_path': path,
                'element_id': node.get('id', ''), 'text': text, 'text_sha256': _hash(text.encode('utf-8'))})
            return
        for child in node:
            if _local(child) not in ('title', 'label'):
                walk(child, section, kind)
    meta = _child(_child(root, 'front'), 'article-meta')
    for abstract in _children(meta, 'abstract'):
        walk(abstract, ['Abstract'], 'abstract')
    body = _child(root, 'body')
    if body is None:
        raise PublicationTextUnavailable('No article body is available; an abstract is not a full-text substitute.')
    walk(body, [], 'body')
    if not any(p['kind'] == 'body' for p in paragraphs):
        raise PublicationTextUnavailable('No supported article-body paragraphs are available; no abstract fallback was retained.')
    warnings = ['Reader includes extracted abstract and body paragraphs only. Tables, figure images/captions, references, footnotes and supplements are not read here.',
        'Paragraphs containing formulas, unsupported media, embedded footnotes, reference lists or captions are omitted whole; consult the original article for those passages.',
        'Whitespace and inline typography are normalized. Superscripts and subscripts use ^{…} and _{…}; visual layout is not preserved. Inspect the original when formatting affects meaning.']
    return paragraphs, {'paragraph_count': len(paragraphs), 'included_scope': ['abstract paragraphs', 'body paragraphs'],
        'omitted_counts': omitted, 'warnings': warnings}


def fetch(pmcid, *, transport=None):
    """Fetch one public OA article through three bounded, sequential requests."""
    requested = normalize_pmcid(pmcid)
    deadline = time.monotonic() + TOTAL_TIMEOUT
    def request(url, limit):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PublicationTextError('The article retrieval deadline expired.')
        try:
            response = (transport or _network)(url, timeout=min(REQUEST_TIMEOUT, remaining), max_bytes=limit)
        except (OSError, TimeoutError):
            raise PublicationTextError('The public article service could not be reached within the request limit.') from None
        if not isinstance(response, references.Response) or type(response.status) is not int or not isinstance(response.body, bytes) or not isinstance(response.headers, dict):
            raise PublicationTextProtocolError('The article transport returned an invalid response.')
        if len(response.body) > limit:
            raise PublicationTextProtocolError('The article service response exceeds its size limit.')
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        if headers.get('content-encoding', 'identity').lower() not in ('', 'identity'):
            raise PublicationTextProtocolError('Compressed article responses are not accepted.')
        if 300 <= response.status < 400:
            raise PublicationTextProtocolError('Article service redirects are refused.')
        if response.status == 429:
            raise references.ReferenceRateLimited(references._retry_after(headers))
        if response.status in (403, 404):
            raise PublicationTextUnavailable('This article/version is not available through the supported PMC Open Access dataset.')
        if response.status != 200:
            raise PublicationTextError(f'The public article service returned HTTP {response.status}.')
        return response.body
    identity_url = _IDENTITY + '?' + urlencode({'ids': requested, 'versions': 'yes', 'showaiid': 'yes', 'format': 'json', 'tool': 'grantstudio_publication_reader'})
    identity_bytes = request(identity_url, MAX_METADATA_BYTES)
    identity, selected, selection = _identity(_json(identity_bytes), requested)
    version, base = selected['pmcid'], requested.split('.')[0]
    metadata_url = _CLOUD + '/metadata/' + version + '.json'
    metadata_bytes = request(metadata_url, MAX_METADATA_BYTES)
    metadata = _json(metadata_bytes)
    if metadata.get('pmcid') != base or type(metadata.get('version')) is not int or metadata['version'] != int(version.split('.')[1]):
        raise PublicationTextProtocolError('Cloud metadata does not match the selected PMC version.')
    code = metadata.get('license_code')
    if metadata.get('is_pmc_openaccess') is not True or not isinstance(code, str) or code not in _LICENSES:
        raise PublicationTextUnavailable('Only explicitly open-access PMC articles with supported Creative Commons terms can be retained. TDM-only or unknown terms are not treated as open access.')
    expected_doi = _doi(selected.get('doi') or identity.get('doi'))
    doi, pmid, expected_pmid = _doi(metadata.get('doi')), _pmid(metadata.get('pmid')), _pmid(identity.get('pmid'))
    if (expected_doi and doi and expected_doi != doi) or (expected_pmid and pmid and expected_pmid != pmid):
        raise PublicationTextProtocolError('Cloud metadata conflicts with PMC article identifiers.')
    canonical = {'pmcid': base, 'pmid': expected_pmid or pmid, 'doi': expected_doi or doi}
    provider_xml_url = metadata.get('xml_url')
    if not isinstance(provider_xml_url, str) or len(provider_xml_url) > 2000:
        raise PublicationTextProtocolError('The provider did not supply a valid XML object URL.')
    parts = urlsplit(provider_xml_url)
    query = parse_qs(parts.query, keep_blank_values=True)
    checksum = query.get('md5', [])
    if ((parts.scheme, parts.netloc) not in {('https', 'pmc-oa-opendata.s3.amazonaws.com'), ('s3', 'pmc-oa-opendata')}
            or parts.path != f'/{version}/{version}.xml' or parts.fragment
            or set(query) != {'md5'} or len(checksum) != 1 or not re.fullmatch(r'[a-fA-F0-9]{32}', checksum[0])):
        raise PublicationTextProtocolError('The XML URL is not the exact selected official article object with a checksum.')
    # Cloud metadata uses S3 object URLs; access that exact validated object
    # anonymously over the fixed official HTTPS endpoint, never a generic proxy.
    xml_url = _CLOUD + parts.path + '?' + urlencode({'md5': checksum[0]})
    xml_bytes = request(xml_url, MAX_XML_BYTES)
    if hashlib.md5(xml_bytes, usedforsecurity=False).hexdigest() != checksum[0].lower():
        raise PublicationTextProtocolError('The XML checksum differs from the frozen provider metadata; fetch a fresh snapshot explicitly.')
    root = _xml(xml_bytes)
    article_meta = _check_xml_identity(root, canonical, version, selected.get('aiid'))
    license_record = _license(article_meta, code)
    paragraphs, coverage = _extract(root)
    title = _text(_child(_child(article_meta, 'title-group'), 'article-title'))
    if not title or len(title) > 4000:
        raise PublicationTextProtocolError('The article has no usable title.')
    authors = []
    for group in _children(article_meta, 'contrib-group'):
        for person in _children(group, 'contrib'):
            if person.get('contrib-type', 'author') == 'author':
                name = _child(person, 'name')
                author = ' '.join(filter(None, [_text(_child(name, 'given-names')), _text(_child(name, 'surname'))])) or _text(_child(person, 'collab'))
                if author:
                    authors.append(author)
    if len(authors) > 1000:
        raise PublicationTextUnavailable('The article author list exceeds the reader limit.')
    journal_meta = _child(_child(root, 'front'), 'journal-meta')
    journal = _text(_child(_child(journal_meta, 'journal-title-group'), 'journal-title')) or _text(_child(journal_meta, 'journal-title'))
    year = next((_text(_child(date, 'year')) for date in _children(article_meta, 'pub-date') if re.fullmatch(r'[12][0-9]{3}', _text(_child(date, 'year')))), None)
    notices = {'retracted': _bool(metadata.get('is_retracted')), 'is_manuscript': _bool(metadata.get('is_manuscript')),
        'scope': 'PMC Cloud metadata for this article version at retrieval; correction and concern notices were not searched.',
        'limitations': ['A false or missing retraction flag is not a comprehensive check for corrections, expressions of concern or later changes.']}
    limitations = ['Downloaded text is not a scientific-support judgment or proof that the whole article was read.',
        'This is a dated NLM/PMC snapshot, not a live guarantee of the latest text or publication status.',
        'Retained passages remain subject to the reported license, attribution and any separate third-party rights.',
        'Only explicitly retained paragraphs enter proposal evidence; article bodies are not automatically sent to the drafting model.']
    if license_record['source'] == 'metadata_only':
        limitations.append('License terms are reported by PMC Cloud metadata only; no license statement or URL was present in the retained XML.')
    return {'status': 'retrieved', 'requested_pmcid': requested, 'canonical': canonical, 'article_version': version,
        'article_type': root.get('article-type', ''), 'title': title, 'authors': authors, 'journal': journal,
        'year': int(year) if year else None, 'provider': 'ncbi_pmc_cloud',
        'source_url': 'https://pmc.ncbi.nlm.nih.gov/articles/' + version + '/',
        'retrieved_at': datetime.now(timezone.utc).isoformat(), 'license': license_record,
        'publication_notices': notices, 'coverage': coverage, 'paragraphs': paragraphs,
        'provenance': {'identity_url': identity_url, 'metadata_url': metadata_url, 'xml_url': xml_url,
            'provider_xml_url': provider_xml_url,
            'identity_sha256': _hash(identity_bytes), 'metadata_sha256': _hash(metadata_bytes),
            'xml_sha256': _hash(xml_bytes), 'provider_md5': checksum[0].lower(),
            'extractor_version': EXTRACTOR_VERSION, 'version_selection': selection,
            'current_version': _bool(selected.get('current'))},
        'raw_xml_b64': base64.b64encode(xml_bytes).decode('ascii'), 'limitations': limitations}
