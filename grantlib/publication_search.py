"""Live PubMed discovery for candidate references, with NIH iCite influence metrics.

Official formats consulted 2026-10-31:
https://www.ncbi.nlm.nih.gov/books/NBK25499/   (E-utilities esearch/efetch)
https://icite.od.nih.gov/api                     (iCite /api/pubs)

Only the explicit public search terms leave the machine, followed by the PMIDs
PubMed returned. Results are discovery: nothing is retained as proposal evidence
until the connected model resolves a chosen PMID and reads it. Ranking is
PubMed's own Best Match order; iCite metrics are reported, not used to reorder.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlencode
import xml.etree.ElementTree as ET

from . import references

API_BASE = 'https://eutils.ncbi.nlm.nih.gov/entrez/eutils/'
ICITE_BASE = 'https://icite.od.nih.gov/api/pubs'
HOSTS = {'eutils.ncbi.nlm.nih.gov', 'icite.od.nih.gov'}
MAX_BYTES = 4 * 1024 * 1024
TIMEOUT = 15
MAX_RESULTS = 20
SORTS = {'relevance': 'relevance', 'newest': 'pub_date', 'most_cited': 'relevance'}
LIMITATIONS = [
    'Results follow PubMed Best Match (or publication date) for the supplied terms; they are not a systematic review.',
    'iCite citation_count and relative_citation_ratio measure influence within PubMed, not correctness or relevance to a claim.',
    'A candidate becomes citable evidence only after grants_resolve_reference and reading its abstract or full text.',
    'Retraction and correction flags come from the PubMed record only; linked notices were not fetched.',
]


class PublicationSearchError(RuntimeError):
    """Search failure without raw public response content."""


def _network(url, *, timeout, max_bytes):
    return references._request_official(url, timeout=timeout, max_bytes=max_bytes, hosts=HOSTS,
                                        user_agent='GrantStudio-ReferenceFinder/1.0 (explicit public terms)')


def check_query(query):
    if not isinstance(query, str) or not 2 <= len(query.strip()) <= 500 or '\n' in query or '\r' in query:
        raise ValueError('Supply one line of explicit public search terms (2 to 500 characters).')
    if re.search(r'\[\[|https?://|\\\\|/Users/|/home/', query):
        raise ValueError('Search terms must be public topic words, identifiers or PubMed field tags; not citations, URLs or paths.')
    return ' '.join(query.split())


def _term(query, min_year, max_year):
    if min_year or max_year:
        return f'({query}) AND ({min_year or 1800}[dp] : {max_year or 3000}[dp])'
    return query


def _get(transport, url):
    try:
        response = transport(url, timeout=TIMEOUT, max_bytes=MAX_BYTES)
    except references.ReferenceRateLimited:
        raise
    except (references.ReferenceError, OSError, TimeoutError):
        raise PublicationSearchError('The public publication service could not be reached within the request limit.') from None
    if (not isinstance(response, references.Response) or type(response.status) is not int
            or not isinstance(response.body, bytes)):
        raise PublicationSearchError('The search transport returned an invalid response.')
    if response.status == 429:
        raise references.ReferenceRateLimited(references._retry_after(
            {str(k).lower(): str(v) for k, v in (response.headers or {}).items()}))
    if response.status != 200:
        raise PublicationSearchError(f'The public publication service returned HTTP {response.status}.')
    return response.body


def _esearch(transport, query, limit, sort, min_year, max_year):
    url = API_BASE + 'esearch.fcgi?' + urlencode({
        'db': 'pubmed', 'retmode': 'json', 'term': _term(query, min_year, max_year),
        'retmax': limit, 'retstart': 0, 'sort': SORTS[sort], 'tool': 'grantstudio_reference_finder'})
    try:
        data = json.loads(_get(transport, url))
        result = data['esearchresult']
        ids = [i for i in result['idlist'] if references._known_id(i, 'pmid')]
        total = int(result.get('count', len(ids)))
    except (ValueError, KeyError, TypeError, UnicodeError):
        raise PublicationSearchError('PubMed returned an invalid or incomplete search result.') from None
    return ids, total, url


def _efetch(transport, ids):
    url = API_BASE + 'efetch.fcgi?' + urlencode({
        'db': 'pubmed', 'id': ','.join(ids), 'retmode': 'xml', 'tool': 'grantstudio_reference_finder'})
    raw = _get(transport, url)
    upper = raw.upper()
    if b'<!ENTITY' in upper or re.search(br'<!DOCTYPE[^>]*\[', upper):
        raise PublicationSearchError('PubMed XML contains unsupported entity declarations.')
    try:
        root = ET.fromstring(raw)
        count = 0
        stack = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            count += 1
            if count > 250000 or depth > 100:
                raise ValueError('structure')
            stack.extend((child, depth + 1) for child in node)
        if root.tag != 'PubmedArticleSet':
            raise ValueError('document')
        records = {}
        for item in root:
            if item.tag not in ('PubmedArticle', 'PubmedBookArticle'):
                raise ValueError('element')
            primary = item.find('MedlineCitation' if item.tag == 'PubmedArticle' else 'BookDocument')
            pmid = references._known_id(references._text(primary.find('PMID')) if primary is not None else None, 'pmid')
            if pmid not in ids or pmid in records:
                raise ValueError('pmid')
            wrapped = ('<PubmedArticleSet>' + ET.tostring(item, encoding='unicode') + '</PubmedArticleSet>').encode('utf-8')
            snapshot = references._snapshot({'kind': 'pmid', 'value': pmid}, 'pubmed', url, wrapped)
            references._pubmed(wrapped, snapshot)
            if snapshot['status'] == 'resolved':
                records[pmid] = snapshot
    except (ET.ParseError, ValueError, TypeError, RecursionError, references.ReferenceError):
        raise PublicationSearchError('PubMed returned invalid or mismatched article XML.') from None
    return records


def _icite(transport, ids):
    """Citation influence for the returned PMIDs; failures degrade to a warning."""
    url = ICITE_BASE + '?' + urlencode({'pmids': ','.join(ids), 'fl': 'pmid,citation_count,relative_citation_ratio,nih_percentile,is_research_article,is_clinical,year'})
    try:
        data = json.loads(_get(transport, url))
        rows = data['data'] if isinstance(data, dict) else data
        metrics = {}
        for row in rows:
            pmid = references._known_id(str(row.get('pmid')), 'pmid')
            if pmid in ids:
                metrics[pmid] = {k: row.get(k) for k in ('citation_count', 'relative_citation_ratio', 'nih_percentile',
                                                         'is_research_article', 'is_clinical')}
        return metrics, None
    except (PublicationSearchError, references.ReferenceRateLimited, ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return {}, 'iCite influence metrics were unavailable for this search; candidates are shown without citation counts.'


def _candidate(rank, snapshot, metrics):
    notices = snapshot.get('publication_notices') or {}
    flags = sorted(k for k in ('retracted', 'has_correction', 'expression_of_concern', 'is_retraction_notice',
                               'is_correction_notice', 'is_expression_of_concern_notice') if notices.get(k))
    authors = snapshot.get('authors') or []
    return {'rank': rank, 'pmid': snapshot['identifiers']['pmid'], 'doi': snapshot['identifiers'].get('doi'),
            'title': snapshot.get('title'), 'first_authors': authors[:3], 'author_count': len(authors),
            'journal': snapshot.get('journal_abbreviation') or snapshot.get('journal'), 'year': snapshot.get('year'),
            'publication_types': [t for t in snapshot.get('publication_types', []) if t != 'Journal Article'],
            'notice_flags': flags, **metrics.get(snapshot['identifiers']['pmid'], {}),
            'abstract': snapshot.get('abstract'), 'pubmed_url': snapshot.get('source_url')}


def search(query, *, limit=10, sort='relevance', min_year=None, max_year=None, transport=None):
    """Find candidate references on PubMed; returns ordered candidates plus provenance and limits."""
    query = check_query(query)
    if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f'limit must be an integer from 1 to {MAX_RESULTS}.')
    if sort not in SORTS:
        raise ValueError('sort must be relevance, newest or most_cited.')
    for label, year in (('min_year', min_year), ('max_year', max_year)):
        if year is not None and (type(year) is not int or not 1800 <= year <= 2100):
            raise ValueError(f'{label} must be a four-digit year.')
    transport = transport or _network
    fetch_limit = min(MAX_RESULTS, limit * 2) if sort == 'most_cited' else limit
    ids, total, search_url = _esearch(transport, query, fetch_limit, sort, min_year, max_year)
    warnings = []
    records = _efetch(transport, ids) if ids else {}
    metrics, warning = _icite(transport, ids) if ids else ({}, None)
    if warning:
        warnings.append(warning)
    missing = [i for i in ids if i not in records]
    if missing:
        warnings.append(f'{len(missing)} returned PMID(s) had no resolvable article record and were omitted.')
    ordered = [records[i] for i in ids if i in records]
    if sort == 'most_cited':
        ordered.sort(key=lambda s: -(metrics.get(s['identifiers']['pmid'], {}).get('citation_count') or 0))
        ordered = ordered[:limit]
    candidates = [_candidate(index + 1, snapshot, metrics) for index, snapshot in enumerate(ordered)]
    return {'query': query, 'sort': sort, 'total_matches': total, 'returned': len(candidates),
            'candidates': candidates, 'warnings': warnings, 'limitations': list(LIMITATIONS),
            'provenance': {'search_url': search_url, 'provider': 'pubmed', 'metrics_provider': 'icite' if metrics else None},
            'next_step': 'Choose candidates whose abstract supports the intended claim, then call grants_resolve_reference with the PMID to add each to the proposal library before citing it as [[R_...]].'}
