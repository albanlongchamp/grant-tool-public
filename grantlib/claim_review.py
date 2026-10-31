"""Version-specific, explicitly authored claim judgments and paper snapshots.

Resolving a paper is not verifying a scientific claim. Judgments retain the text
read, its scope and the reviewer; neither generated claims nor old judgments
become automatically verified evidence in another draft.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from difflib import SequenceMatcher

from . import publication_store


VERDICTS = {'supported', 'partial', 'contradicted', 'unresolved'}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat()


def initialize(connection):
    connection.executescript('''
    CREATE TABLE IF NOT EXISTS reference_records (
      id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES proposals(id),
      snapshot TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS manual_claims (
      id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
      quote TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(artifact_id,quote));
    CREATE TABLE IF NOT EXISTS claim_assessments (
      id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
      claim_id TEXT NOT NULL, assessment TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS claims_by_version ON manual_claims(artifact_id);
    CREATE INDEX IF NOT EXISTS assessments_by_claim ON claim_assessments(artifact_id,claim_id);
    ''')


def references(connection, proposal_id):
    return [json.loads(row['snapshot']) for row in connection.execute(
        'SELECT snapshot FROM reference_records WHERE proposal_id=? ORDER BY created_at DESC,rowid DESC', (proposal_id,))]


def claims(connection, artifact):
    structured = artifact.get('structured') or {}
    if isinstance(structured, str):
        structured = json.loads(structured)
    result = []
    for index, value in enumerate(structured.get('claims', [])):
        result.append({'id': _hash([artifact['id'], index, value]),
                       'text': value['text'], 'basis': value['basis'],
                       'source_ids': value.get('source_ids', []), 'origin': 'model', 'quote': ''})
    for row in connection.execute('SELECT * FROM manual_claims WHERE artifact_id=? ORDER BY created_at,rowid', (artifact['id'],)):
        result.append({'id': row['id'], 'text': row['quote'], 'quote': row['quote'],
                       'basis': 'needs_verification', 'source_ids': [], 'origin': 'draft_quote'})
    judgments = {}
    for row in connection.execute('SELECT * FROM claim_assessments WHERE artifact_id=? ORDER BY created_at DESC,rowid DESC', (artifact['id'],)):
        judgments.setdefault(row['claim_id'], []).append(json.loads(row['assessment']))
    for claim in result:
        claim['assessments'] = judgments.get(claim['id'], [])
    return result


def add_reference(store, proposal_id, expected_revision, snapshot):
    if not isinstance(snapshot, dict) or snapshot.get('status') != 'resolved':
        status = snapshot.get('status', 'unavailable') if isinstance(snapshot, dict) else 'unavailable'
        raise ValueError('The paper identity was not resolved (' + status + '). Check the exact PMID or DOI.')
    if not isinstance(snapshot.get('title'), str) or not snapshot['title'].strip():
        raise ValueError('The resolved reference has no title.')
    if len(_json(snapshot)) > 200000:
        raise ValueError('The reference record exceeds the supported size.')
    record = json.loads(_json(snapshot))
    record['id'] = 'R_' + uuid.uuid4().hex
    record['canonical'] = record.get('canonical') or record.get('identifiers') or {}
    record['notices'] = record.get('notices') or record.get('publication_notices')
    record['verification'] = {'metadata_only': True, 'semantic_support': False, 'full_text': False}
    with store.connect(True) as c:
        store._editable(c, proposal_id, expected_revision)
        if c.execute('SELECT COUNT(*) FROM reference_records WHERE proposal_id=?', (proposal_id,)).fetchone()[0] >= 200:
            raise ValueError('This proposal has reached its reference snapshot limit.')
        c.execute('INSERT INTO reference_records VALUES (?,?,?,?)', (record['id'], proposal_id, _json(record), _now()))
        store._bump(c, proposal_id)
    return store.get(proposal_id)


def add_claim(store, proposal_id, expected_revision, artifact_id, quote):
    if not isinstance(quote, str) or not 0 < len(quote.strip()) <= 8000:
        raise ValueError('Use an exact draft quotation of 1 to 8,000 characters.')
    quote = quote.strip()
    with store.connect(True) as c:
        store._editable(c, proposal_id, expected_revision)
        artifact = store._base(c, proposal_id, artifact_id)
        if artifact is None or quote not in artifact['markdown']:
            raise ValueError('The quotation must appear exactly in the selected saved version.')
        claim_id = _hash([artifact_id, 'human_quote', quote])
        existed = c.execute('SELECT 1 FROM manual_claims WHERE id=?', (claim_id,)).fetchone()
        if not existed:
            c.execute('INSERT INTO manual_claims VALUES (?,?,?,?)', (claim_id, artifact_id, quote, _now()))
            store._bump(c, proposal_id)
    return store.get(proposal_id)


def reference_source(reference, *, limit=None):
    abstract = reference.get('abstract') or ''
    if not isinstance(abstract, str):
        raise ValueError('The reference abstract is not readable text.')
    text = abstract[:limit] if limit is not None else abstract
    canonical = reference.get('canonical') or reference.get('identifiers') or {}
    return {'id': reference['id'], 'layer': 'publication', 'title': reference['title'],
            'text': text or 'No abstract was retrieved. Bibliographic identity only; no claim support has been assessed.',
            'source_uri': reference.get('source_url', ''),
            'metadata': {'role': 'literature', 'evidence_status': 'publication_not_independently_verified',
                         'locator': 'Abstract' if abstract else 'Bibliographic metadata',
                         'scope': 'abstract' if abstract else 'bibliographic_metadata',
                         'canonical': canonical, 'authors': reference.get('authors', []),
                         'journal': reference.get('journal'), 'year': reference.get('year'),
                         'retrieved_at': reference.get('retrieved_at'), 'provider': reference.get('provider'),
                         'notices': reference.get('notices'), 'limitations': reference.get('limitations', []),
                         'truncated': len(text) < len(abstract), 'total_text_chars': len(abstract),
                         'included_text_chars': len(text)}}


def _warnings(source):
    if source is None:
        return ['No evidence source was selected.']
    metadata = source.get('metadata', {})
    result = ['This is a recorded reviewer judgment, not independent confirmation of the science.']
    if source['layer'] == 'publication':
        if metadata.get('scope') == 'full_text_excerpt':
            result.append('This judgment applies only to the retained full-text extracted paragraph; the complete article and claim support have not been independently verified.')
        else:
            result.append('Only the retained abstract was read here; full-text methods, results and limitations have not been verified.')
        if metadata.get('notices'):
            result.append('Inspect the publication-notice metadata and provider coverage limits.')
    if source['layer'] == 'public_project':
        result.append('A funded-project description does not establish completed findings or efficacy.')
    if source['layer'] == 'investigator_memory':
        result.append('This is a dated investigator statement within its recorded scope, not independent research evidence or a current availability check.')
    if source['layer'] == 'investigator_decision':
        result.append('This is a recorded proposal instruction or constraint, not independent research evidence or an availability check.')
    if metadata.get('evidence_status') in ('proposed', 'mixed_or_unclear', 'reported_result_unverified'):
        result.append('The source is labeled ' + metadata['evidence_status'].replace('_', ' ') + '; retain that distinction in the claim.')
    if metadata.get('role') in ('ownership_uncertain', 'collaborator_or_template'):
        result.append('Source ownership does not establish applicant-owned preliminary evidence.')
    return result


def assess(store, proposal_id, expected_revision, artifact_id, claim_id, *, source_id='',
           verdict, excerpt='', locator='', note='', reviewer='investigator'):
    if verdict not in VERDICTS or reviewer not in ('investigator', 'assistant'):
        raise ValueError('Choose a supported judgment and reviewer type.')
    values = {'source_id': (source_id, 300), 'excerpt': (excerpt, 12000),
              'locator': (locator, 2000), 'note': (note, 8000)}
    for name, (value, maximum) in values.items():
        if not isinstance(value, str) or len(value) > maximum:
            raise ValueError(f'{name} must be text of at most {maximum} characters.')
    source_id, excerpt, locator, note = (v.strip() for v in (source_id, excerpt, locator, note))
    if not note or (verdict != 'unresolved' and not all((source_id, excerpt, locator))):
        raise ValueError('Explain the judgment. A support or contradiction judgment also requires a source, exact excerpt and locator.')
    if not isinstance(claim_id, str) or not re.fullmatch(r'[a-f0-9]{64}', claim_id):
        raise ValueError('Select a claim from this saved version.')
    with store.connect(True) as c:
        store._editable(c, proposal_id, expected_revision)
        row = store._base(c, proposal_id, artifact_id)
        if row is None:
            raise ValueError('Select a saved version.')
        artifact = dict(row)
        claim = next((item for item in claims(c, artifact) if item['id'] == claim_id), None)
        if claim is None:
            raise ValueError('The claim does not belong to this saved version.')
        packet = store._packet(c, row['context_id'])
        source = next((s for s in packet.get('sources', []) if s['id'] == source_id), None)
        scope = 'source_excerpt' if source else 'no_source_selected'
        reviewable = source.get('text', '') if source else ''
        if source is None and source_id:
            source = next((s for s in publication_store.passages(c, proposal_id) if s['id'] == source_id), None)
            if source:
                reviewable = source['text']
                scope = source['metadata']['scope']
        if source is None and source_id:
            reference = next((r for r in references(c, proposal_id) if r['id'] == source_id), None)
            if reference:
                source = reference_source(reference)
                reviewable = reference.get('abstract') or ''
                scope = 'abstract' if reviewable else 'bibliographic_metadata'
        elif source and source['layer'] == 'publication':
            scope = source.get('metadata', {}).get('scope', 'abstract')
            if scope == 'bibliographic_metadata':
                reviewable = ''
        if source_id and source is None:
            raise ValueError('Select evidence retained by this version or a publication reference or paragraph retained for this proposal.')
        if excerpt and (not reviewable or excerpt not in reviewable):
            raise ValueError('The excerpt must match the retained source text or publication paragraph exactly.')
        if verdict != 'unresolved' and not reviewable:
            raise ValueError('Bibliographic metadata alone cannot support a scientific judgment. Read an evidence passage first.')
        item = {'id': uuid.uuid4().hex, 'claim_id': claim_id, 'claim_text': claim['text'],
                'source_id': source_id, 'verdict': verdict, 'excerpt': excerpt, 'locator': locator,
                'note': note, 'reviewer': reviewer, 'created_at': _now(), 'scope': scope,
                'source_snapshot': source, 'source_hash': _hash(source) if source else None,
                'warnings': _warnings(source)}
        c.execute('INSERT INTO claim_assessments VALUES (?,?,?,?,?)',
                  (item['id'], artifact_id, claim_id, _json(item), item['created_at']))
        store._bump(c, proposal_id)
    return store.get(proposal_id)


def latest_assessments(claim):
    """Keep the most recent judgment per evidence source without erasing history."""
    seen, result = set(), []
    for item in claim.get('assessments', []):
        if item['source_id'] not in seen:
            seen.add(item['source_id'])
            result.append(item)
    return result


_CITATION = re.compile(r'\[\[([A-Za-z0-9_-]+)\]\]')
_QUEUE_TEXT_LIMIT = 8000
_QUEUE_BYTE_LIMIT = 200000


def _text_view(text, limit):
    """Return an exact prefix, explicitly distinguishing it from the full text."""
    return {'text': text[:limit], 'total_chars': len(text), 'truncated': len(text) > limit,
            'sha256': hashlib.sha256(text.encode()).hexdigest()}


def _paragraphs(markdown):
    """Conservative exact prose spans; headings/code/quotes are not claim detection."""
    from .draft_metrics import _line_states
    start, end = None, None
    for offset, line, outside in _line_states(markdown):
        prose = outside and bool(line.strip()) and not re.match(r'^\s*(?:#{1,6}\s|[-=*]{3,}\s*$)', line)
        if not prose:
            if start is not None:
                yield from _split_span(markdown, start, end)
            start, end = None, None
        else:
            if start is None:
                start = offset + len(line) - len(line.lstrip())
            end = offset + len(line.rstrip())
    if start is not None:
        yield from _split_span(markdown, start, end)


def _split_span(markdown, start, end):
    while start < end:
        stop = min(start + _QUEUE_TEXT_LIMIT, end)
        if stop < end:
            boundary = markdown.rfind(' ', start + _QUEUE_TEXT_LIMIT // 2, stop)
            if boundary > start:
                stop = boundary
        text = markdown[start:stop]
        if re.search(r'\w', _CITATION.sub('', text)):
            yield {'text': text, 'spans': [{'start': start, 'end': stop}]}
        start = stop
        while start < end and markdown[start].isspace():
            start += 1


def _exact_spans(markdown, text):
    if not text:
        return []
    return [{'start': match.start(), 'end': match.end()}
            for match in re.finditer(re.escape(text), markdown)][:20]


def _queue_candidates(artifact):
    markdown = artifact.get('markdown') or ''
    result = []
    for claim in artifact.get('claims', []):
        text = claim.get('quote') or claim['text']
        result.append({'text': text, 'spans': _exact_spans(markdown, text),
                       'claim': claim, 'origin': claim['origin']})
    # Imported drafts have no generated claim list. Prose candidates make that
    # absence visible without registering claims, judging truth, or writing state.
    for paragraph in _paragraphs(markdown):
        span = paragraph['spans'][0]
        covered = list(paragraph['text'])
        for candidate in result:
            for known in candidate['spans']:
                if span['start'] <= known['start'] and known['end'] <= span['end']:
                    covered[known['start'] - span['start']:known['end'] - span['start']] = ' ' * (known['end'] - known['start'])
        remaining = _CITATION.sub('', ''.join(covered))
        if re.search(r'\w', remaining):
            result.append({**paragraph, 'claim': None, 'origin': 'prose_candidate'})
    return result


def _assessment_view(assessment):
    source = assessment.get('source_snapshot') or {}
    metadata = source.get('metadata') or {}
    return {key: assessment.get(key) for key in
            ('id', 'claim_id', 'verdict', 'source_id', 'reviewer', 'created_at', 'scope', 'source_hash')} | {
        'excerpt': _text_view(assessment.get('excerpt') or '', 2000),
        'locator': _text_view(assessment.get('locator') or '', 1000),
        'note': _text_view(assessment.get('note') or '', 1000),
        'source': {'id': source.get('id'), 'title': (source.get('title') or '')[:500],
                   'source_uri': (source.get('source_uri') or '')[:2000], 'layer': source.get('layer'),
                   'role': metadata.get('role'), 'evidence_status': metadata.get('evidence_status'),
                   'scope': metadata.get('scope')},
        'judgment_is_independent_verification': False,
    }


def _parent_match(candidate, parent_candidates):
    text = candidate['text']
    exact = [item for item in parent_candidates if item['text'] == text]
    if exact:
        # Prefer a registered claim with a judgment over a fallback paragraph.
        return max(exact, key=lambda item: bool((item.get('claim') or {}).get('assessments'))), 'exact_text'
    words = text.split()[:1500]
    if len(words) < 4:
        return None, None
    best, score = None, .65
    for item in parent_candidates[:1000]:
        old_words = item['text'].split()[:1500]
        if min(len(words), len(old_words)) / max(len(words), len(old_words), 1) < .5:
            continue
        similarity = SequenceMatcher(None, old_words, words, autojunk=True).ratio()
        if similarity > score:
            best, score = item, similarity
    return (best, 'similar_wording_candidate') if best else (None, None)


def review_queue(proposal, artifact_id, *, offset=0, limit=20):
    """Read bounded rule-based triage; no model, writes, or inherited verification.

    Exact registered claims and uncovered prose paragraphs are candidates. A
    lexical parent match is only a review aid, never semantic claim identity.
    """
    if type(offset) is not int or not 0 <= offset <= 1000000:
        raise ValueError('offset must be an integer from 0 to 1000000.')
    if type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError('limit must be an integer from 1 to 20.')
    artifacts = {item['id']: item for item in proposal['artifacts']}
    if artifact_id not in artifacts:
        raise ValueError('Version not found in this proposal.')
    artifact = artifacts[artifact_id]
    parent_id = artifact.get('base_artifact_id')
    parent = artifacts.get(parent_id)
    parent_candidates = _queue_candidates(parent) if parent else []
    sources = {source['id']: source for source in (artifact.get('context') or {}).get('sources', [])}
    candidates = _queue_candidates(artifact)
    queue = []
    for candidate in candidates:
        claim = candidate['claim'] or {}
        current = latest_assessments(claim)
        verdicts = {item['verdict'] for item in current}
        reasons = []
        if not current:
            reasons.append('unreviewed_claim' if claim else 'unreviewed_prose_candidate')
        reasons.extend('recorded_' + verdict for verdict in ('contradicted', 'partial', 'unresolved') if verdict in verdicts)
        if claim and not candidate['spans']:
            reasons.append('claim_not_exactly_located_in_draft')
        inline = list(dict.fromkeys(_CITATION.findall(candidate['text'])))
        linked = list(dict.fromkeys(inline + claim.get('source_ids', [])))
        missing = [source_id for source_id in linked if source_id not in sources]
        if missing:
            reasons.append('citation_not_in_version_packet')
        if not reasons:
            continue
        parent_item, match = _parent_match(candidate, parent_candidates)
        prior = latest_assessments((parent_item or {}).get('claim') or {})
        if parent and candidate['text'] not in parent.get('markdown', ''):
            reasons.append('text_not_exactly_present_in_parent')
        if prior and not current:
            reasons.append('parent_judgment_requires_new_review')
        source_views = []
        for source_id in linked[:20]:
            source = sources.get(source_id)
            metadata = (source or {}).get('metadata') or {}
            source_views.append({'id': source_id, 'retained_by_version': source is not None,
                                 'title': ((source or {}).get('title') or '')[:500],
                                 'layer': (source or {}).get('layer'), 'role': metadata.get('role'),
                                 'evidence_status': metadata.get('evidence_status'),
                                 'locator': str(metadata.get('locator') or '')[:1000]})
        text_view = _text_view(candidate['text'], _QUEUE_TEXT_LIMIT)
        item = {'id': 'Q_' + _hash([artifact_id, claim.get('id'), candidate['text'], candidate['spans']]),
                'claim_id': claim.get('id'), 'origin': candidate['origin'],
                'text': text_view['text'], 'text_window': {k: v for k, v in text_view.items() if k != 'text'},
                'draft_spans': candidate['spans'], 'span_units': 'Python Unicode character offsets; end is exclusive',
                'registration_required': not bool(claim), 'reasons': reasons,
                'priority': 'disputed' if verdicts & {'contradicted', 'partial'} else 'needs_review',
                'inline_source_ids': inline[:20], 'source_ids': linked[:20],
                'source_count': len(linked), 'sources': source_views,
                'citation_observations': ['no_inline_citation'] if not inline else [],
                'missing_source_ids': missing[:20],
                'current_assessments': [_assessment_view(item) for item in current[:4]],
                'current_assessment_count': len(current),
                'parent_comparison': {'artifact_id': parent_id, 'match': match,
                                      'claim_id': ((parent_item or {}).get('claim') or {}).get('id'),
                                      'text': _text_view(parent_item['text'], 2000) if parent_item else None,
                                      'assessments': [_assessment_view(item) for item in prior[:2]],
                                      'assessment_count': len(prior), 'judgments_inherited': False},
                'next_action': ('Register an exact quotation with grants_add_claim, then read evidence and record your judgment.'
                                if not claim else 'Read the exact draft and sources, then record a version-specific judgment.'),
                'semantic_support_verified': False}
        queue.append(item)
    queue.sort(key=lambda item: (item['priority'] != 'disputed',
                                (item['draft_spans'] or [{'start': 10000000}])[0]['start'], item['id']))
    selected, byte_count = [], 0
    for item in queue[offset:offset + limit]:
        size = len(_json(item).encode())
        if selected and byte_count + size > _QUEUE_BYTE_LIMIT:
            break
        selected.append(item)
        byte_count += size
    next_offset = offset + len(selected) if offset + len(selected) < len(queue) else None
    return {'proposal_id': proposal['id'], 'revision': proposal['revision'], 'artifact_id': artifact_id,
            'base_artifact_id': parent_id, 'method': 'exact-claims-and-prose-triage-v1',
            'items': selected, 'total': len(queue), 'offset': offset, 'next_offset': next_offset,
            'candidate_count': len(candidates), 'model_calls': 0, 'read_only': True,
            'limitations': [
                'Rule-based triage does not identify every scientific claim or establish whether evidence supports it.',
                'Prose candidates may contain hypotheses, planned work or other text that does not require a citation.',
                'A missing inline marker is an observation, not a finding that a citation is scientifically required.',
                'Current judgments are recorded reviewer opinions. Prior-version judgments never transfer to this version.',
                'Similar-wording parent matches are lexical candidates, not semantic identity or a complete change map.',
                'Read full draft/evidence with the artifact and evidence tools when a prefix or assessment list is truncated.',
            ]}


def export_reviews(artifact):
    reviewed = [(claim, latest_assessments(claim)) for claim in artifact.get('claims', [])]
    reviewed = [(claim, items) for claim, items in reviewed if items]
    if not reviewed:
        return ''
    lines = ['## Recorded claim judgments', '',
             'Judgments apply to this draft version and the stated reading scope. They do not certify scientific validity.', '']
    for claim, items in reviewed:
        lines += ['Claim: ' + claim['text'], '']
        for item in items:
            source = item.get('source_snapshot')
            label = source['title'] if source else 'No source selected'
            lines += [f"- {item['verdict'].title()} — {label}; {item['scope']}; {item['reviewer']}; {item['created_at']}",
                      '  ' + item['note']]
            if item['excerpt']:
                longest = max((len(run) for run in re.findall(r'`+', item['excerpt'])), default=0)
                fence = '`' * max(3, longest + 1)
                lines += ['  Read excerpt:', '', fence + 'text', item['excerpt'], fence, '', '  Locator: ' + item['locator']]
            if source and source.get('source_uri'):
                lines += ['  Source: ' + source['source_uri']]
            metadata = (source or {}).get('metadata', {})
            memory_source = (source or {}).get('layer') == 'investigator_memory'
            label_keys = ('role', 'evidence_status') if memory_source else ('role', 'evidence_status', 'scope')
            labels = [str(metadata[key]) for key in label_keys if metadata.get(key)]
            if labels:
                lines += ['  Evidence labels: ' + ' / '.join(labels)]
            if memory_source:
                lines += ['  Memory confirmed: ' + metadata.get('confirmed_at', 'Not recorded') +
                          ' · Review on: ' + (metadata.get('review_after') or 'Not specified')]
                for key in ('scope', 'provenance_note', 'selection_note'):
                    value = metadata.get(key)
                    if value:
                        fence = '`' * max(3, max((len(run) for run in re.findall(r'`+', value)), default=0) + 1)
                        lines += ['', '  ' + key.replace('_', ' ').title() + ':', '', fence + 'text', value, fence, '']
            if (source or {}).get('layer') == 'investigator_decision':
                lines += ['  Proposal decision recorded: ' + metadata.get('recorded_at', 'Not recorded')]
            publication_notes = publication_store.source_notes(source or {})
            if publication_notes:
                lines += ['', publication_notes, '']
            for warning in item.get('warnings', []):
                lines += ['  Review limitation: ' + warning]
        lines.append('')
    return '\n'.join(lines)
