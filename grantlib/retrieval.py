"""Build a bounded, source-linked context packet for proposal development.

All searches run against local snapshots. Query expansion is lexical, using
explicit configured aliases; it is not translation or evidence verification.
Original passages are never rewritten. The assistant must interpret their
context, ownership, tense and limitations before using them in a draft.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from itertools import combinations
import json
from pathlib import Path
import re
import sqlite3
import unicodedata

from . import guidance, library


# Built-in concepts are limited to common method names. Archive-specific
# concepts, including terms in other languages, are configured under
# "vocabulary" -> "synonyms" and read from the library snapshot.
CONCEPTS = {
    'spatial_transcriptomics': ('spatial transcriptomics', 'spatial gene expression', 'visium'),
    'single_cell': ('single cell', 'single-cell', 'scrna-seq', 'single nucleus'),
    'metabolomics': ('metabolomics', 'metabolomic'),
    'biomarkers': ('biomarker', 'biomarkers'),
}
STOP = set('a an the of in for to and or my our we what which show find me about is are with on from '
           'how can could would should will do does did improve improving improvement develop developing '
           'design draft grant proposal research study studies new next best help using use assess investigate '
           'explore exploring compare comparison effect effects role mechanism mechanisms aim aims'.split())
SUBSTANTIVE = {'research_strategy', 'specific_aims', 'full_application', 'project_summary', 'protocol', 'analysis_plan'}
DEPRIORITIZED = {'administration', 'biosketch', 'support_letter', 'references', 'figure_or_data_asset', 'research_administration'}


def _normal(text):
    text = unicodedata.normalize('NFKD', str(text)).casefold()
    text = ''.join(c for c in text if not unicodedata.combining(c))
    return re.sub(r'[^\w]+', ' ', text).strip()


def _words(text):
    return [w for w in _normal(text).split() if w not in STOP and len(w) > 2
            and not re.fullmatch(r'[krfu]\d{2}', w)]


def _has(text, term):
    return (' ' + _normal(term) + ' ') in (' ' + text + ' ')


def _strings(brief, key, maximum=30):
    values = brief.get(key, []) or []
    if not isinstance(values, list) or len(values) > maximum or any(not isinstance(v, str) for v in values):
        raise ValueError(f'{key} must be a list of at most {maximum} strings')
    if any(len(v) > 300 for v in values):
        raise ValueError(f'{key} entries must be at most 300 characters')
    return list(dict.fromkeys(v.strip() for v in values if v.strip()))


def _phrase(value):
    return '"' + _normal(value).replace('"', '""') + '"'


def _plan(brief, table=CONCEPTS):
    keywords = _strings(brief, 'keywords', 20)
    texts = [str(brief.get(k, '') or '') for k in ('question', 'title')] + keywords
    if any(len(t) > 12000 for t in texts):
        raise ValueError('Proposal title/question is too long for retrieval')
    text = _normal(' '.join(texts))
    concepts = [key for key, terms in table.items() if any(_has(text, t) for t in terms)]
    concepts.sort(key=lambda key: next((text.find(_normal(t)) for t in table[key] if _has(text, t)), 0))
    concepts = concepts[:10]
    groups = {key: '(' + ' OR '.join(_phrase(t) for t in dict.fromkeys(table[key])) + ')' for key in concepts}
    concept_words = set(w for key in concepts for term in table[key] for w in _normal(term).split())
    extras = list(dict.fromkeys(w for t in keywords + texts[:2] for w in _words(t) if w not in concept_words))[:8]
    queries, seen = [], set()

    def add(label, expression, reason):
        if expression and expression not in seen and len(queries) < 26:
            queries.append({'label': label, 'fts': expression, 'reason': reason})
            seen.add(expression)

    for value in keywords:
        found = [key for key in concepts if any(_has(_normal(value), t) for t in table[key])]
        remaining = [w for w in _words(value) if w not in concept_words][:4]
        terms = [groups[key] for key in found] + [_phrase(w) for w in remaining]
        add(value, ' AND '.join(terms[:5]), 'explicit_keyword')
    if 1 < len(concepts) <= 5:
        add(' + '.join(concepts), ' AND '.join(groups[c] for c in concepts), 'combined_concepts')
    for left, right in combinations(concepts[:6], 2):
        add(left + ' + ' + right, groups[left] + ' AND ' + groups[right], 'concept_pair')
    for key in concepts:
        add(key, groups[key], 'individual_concept')
    if extras:
        add(' + '.join(extras[:4]), ' AND '.join(_phrase(w) for w in extras[:4]), 'other_scientific_terms')
        for word in extras[:4]:
            if concepts:
                add(concepts[0] + ' + ' + word, groups[concepts[0]] + ' AND ' + _phrase(word), 'entity_and_term')
            elif len(extras) <= 4:
                add(word, _phrase(word), 'individual_term')
    return concepts, extras, queries


def _concept_table(db_path):
    """Return built-in concepts plus synonyms recorded in the library snapshot."""
    with closing(_read_connection(db_path)) as conn:
        row = conn.execute("SELECT value FROM metadata WHERE key='vocabulary'").fetchone()
    synonyms = json.loads(row[0]).get('synonyms', {}) if row else {}
    return {**CONCEPTS, **{key: tuple(terms) for key, terms in synonyms.items()}}


def _read_connection(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _relevance(text, concepts, extras, table=CONCEPTS):
    normalized = _normal(text)
    matches = [key for key in concepts if any(_has(normalized, t) for t in table[key])]
    return 4 * len(matches) + min(3, sum(_has(normalized, t) for t in extras)), matches


def _personal(db_path, queries, projects, concepts, extras, limit, table=CONCEPTS):
    if not limit:
        return [], {'candidate_count': 0, 'matched_queries': []}
    candidates = {}
    matched = []
    with closing(_read_connection(db_path)) as conn:
        for query in queries:
            conditions = ['unit_fts MATCH ?']
            params = [query['fts']]
            if projects:
                conditions.append('d.project IN (' + ','.join('?' for _ in projects) + ')')
                params += projects
            rows = conn.execute(f'''SELECT {library.UNIT_SELECT}, bm25(unit_fts) AS lexical_rank
                FROM units u JOIN documents d ON d.id=u.document_id JOIN unit_fts ON unit_fts.rowid=u.rowid
                WHERE {' AND '.join(conditions)} ORDER BY lexical_rank LIMIT 120''', params).fetchall()
            matched.append({'query': query['label'], 'candidate_count': len(rows), 'at_candidate_limit': len(rows) == 120})
            for position, row in enumerate(rows):
                item = library._decode_unit(row)
                if item['id'] not in candidates:
                    score, concepts_found = _relevance(item['text'], concepts, extras, table)
                    kind = item['document_kind']
                    score += 3 if kind in SUBSTANTIVE else -8 if kind in DEPRIORITIZED else 0
                    cats = set(item['categories'])
                    score += 2 if cats & {'methods', 'aims', 'hypothesis', 'preliminary_results', 'analysis', 'pitfalls', 'reviewer_feedback'} else 0
                    score -= 10 if cats == {'references'} or item['section'].lower() in {'references', 'bibliography'} else 0
                    score -= 5 if len(item['text']) < 60 else 0
                    item.update(_score=score, _concepts=concepts_found, _queries=[], _rank=position)
                    candidates[item['id']] = item
                candidates[item['id']]['_queries'].append(query['label'])
                candidates[item['id']]['_rank'] = min(position, candidates[item['id']]['_rank'])
        # Selected folders can be browsed even when a short or exploratory brief
        # has no identifiable scientific concepts. Do not fall back across scope.
        if projects and not queries:
            rows = conn.execute(f'''SELECT {library.UNIT_SELECT} FROM units u
                JOIN documents d ON d.id=u.document_id
                WHERE d.project IN ({','.join('?' for _ in projects)})
                ORDER BY CASE WHEN d.kind IN ('research_strategy','specific_aims','full_application') THEN 0 ELSE 1 END,u.ordinal
                LIMIT 500''', projects).fetchall()
            for row in rows:
                item = library._decode_unit(row)
                item.update(_score=3 if item['document_kind'] in SUBSTANTIVE else 0, _concepts=[], _queries=['selected_folder_browse'], _rank=0)
                candidates[item['id']] = item
        # Adjacent excerpts are separate sources, each with their own real ID.
        seeds = sorted(candidates.values(), key=lambda x: (-x['_score'], x['_rank'], x['id']))[:8]
        for seed in seeds:
            if not seed.get('section') or seed['document_kind'] in DEPRIORITIZED:
                continue
            for row in conn.execute(f'''SELECT {library.UNIT_SELECT} FROM units u JOIN documents d ON d.id=u.document_id
                WHERE u.document_id=? AND u.ordinal IN (?,?) AND u.section=?''',
                (seed['document_id'], seed['ordinal'] - 1, seed['ordinal'] + 1, seed['section'])):
                item = library._decode_unit(row)
                if item['id'] in candidates or len(item['text']) < 60 or item['categories'] == ['references']:
                    continue
                item.update(_score=seed['_score'] - 3, _concepts=[], _queries=['adjacent_context'], _rank=120,
                            _adjacent_to=seed['id'])
                candidates[item['id']] = item
    remaining = list(candidates.values())
    selected, seen_text, docs, folders, categories = [], set(), Counter(), Counter(), Counter()
    while remaining and len(selected) < limit:
        eligible = [x for x in remaining if docs[x['document_id']] < 3 and x['text_hash'] not in seen_text]
        if not eligible:
            break
        item = max(eligible, key=lambda x: (x['_score'] + min(2, len(x['_queries']) / 3)
            - 3 * docs[x['document_id']] - .5 * folders[x['project']]
            - .4 * min((categories[c] for c in x['categories']), default=0), -x['_rank'], x['id']))
        selected.append(item)
        remaining.remove(item)
        seen_text.add(item['text_hash']); docs[item['document_id']] += 1; folders[item['project']] += 1
        categories.update(item['categories'])
    result = []
    for item in selected:
        metadata = {key: item.get(key) for key in ('document_id', 'project', 'role', 'evidence_status', 'locator', 'section',
            'cycle', 'categories', 'topics', 'techniques', 'models', 'review_flags', 'review_note', 'identifiers', 'relative_path', 'extraction_status')}
        metadata.update(unit_id=item['id'], kind=item['document_kind'], unit_kind=item['kind'], quoted_verbatim=True,
                        matched_queries=item['_queries'][:5], matched_concepts=item['_concepts'])
        if item.get('_adjacent_to'):
            metadata['context_for'] = 'P_' + item['_adjacent_to']
        result.append({'id': 'P_' + item['id'], 'layer': 'personal', 'title': item['title'], 'text': item['text'],
                       'source_uri': item['source_uri'], 'metadata': metadata})
    return result, {'candidate_count': len(candidates), 'matched_queries': matched}


def _public(db_path, queries, concepts, extras, limit, table=CONCEPTS):
    path = Path(db_path).parent / 'nih.sqlite3'
    if not path.is_file() or not limit:
        return [], {'available': path.is_file(), 'candidate_count': 0, 'coverage': 'imported_records_only'}
    candidates = {}
    with closing(_read_connection(path)) as conn:
        record_count = conn.execute('SELECT count(*) FROM nih_records').fetchone()[0]
        for query in queries[:20]:
            for row in conn.execute('''SELECT r.* FROM nih_records r JOIN nih_fts ON nih_fts.rowid=r.rowid
                WHERE nih_fts MATCH ? AND trim(r.abstract)<>'' ORDER BY bm25(nih_fts),r.fiscal_year DESC LIMIT 80''', (query['fts'],)):
                item = dict(row)
                score, _ = _relevance(item['title'] + ' ' + item['abstract'], concepts, extras, table)
                item['_score'] = score
                candidates[item['application_id']] = item
    selected, cores = [], set()
    for item in sorted(candidates.values(), key=lambda x: (-x['_score'], -int(x.get('fiscal_year') or 0), str(x['application_id']))):
        core = item.get('core_project_number') or item['application_id']
        if core in cores:
            continue
        cores.add(core)
        metadata = {key: item.get(key) for key in ('application_id', 'core_project_number', 'project_number', 'fiscal_year',
                    'activity', 'ic', 'organization', 'principal_investigators', 'retrieved_at', 'source_id', 'subproject_id')}
        metadata.update(evidence_status='funded_project_description', quoted_verbatim=True,
                        interpretation='Funded work description; not verified findings, full application, or evidence of funding causality.')
        selected.append({'id': 'N_' + str(item['application_id']), 'layer': 'public_project', 'title': item['title'],
                         'text': item['abstract'], 'source_uri': item['reporter_url'], 'metadata': metadata})
        if len(selected) >= limit:
            break
    return selected, {'available': True, 'record_count': record_count, 'candidate_count': len(candidates),
                      'coverage': 'imported_records_only', 'annual_records_grouped_by_core_project': True}


def _official(brief, concepts, extras, limit, table=CONCEPTS):
    scope = str(brief.get('mechanism') or brief.get('funder') or '').strip()
    raw_due = str(brief.get('due_date') or '').strip()
    try:
        reference_date = date.fromisoformat(raw_due) if raw_due else date.today()
    except ValueError as error:
        raise ValueError('due_date must be an ISO calendar date') from error
    if not scope or not limit:
        return [], {'scope': scope, 'candidate_count': 0, 'future_policy_ids_omitted': []}
    records = guidance.search_guidance(mechanism=scope)
    candidates, future = [], []
    question = ' '.join(str(brief.get(k) or '') for k in ('question', 'title', 'notes'))
    special = set(_words(question)) & {'clinical', 'trial', 'human', 'animal', 'mouse', 'mice', 'mentor', 'training', 'resubmission', 'reviewer'}
    for item in records:
        if item.get('effective_from') and date.fromisoformat(item['effective_from']) > reference_date:
            future.append(item['id']); continue
        search_text = ' '.join([item['title'], *item['tags'], *item['tips']])
        relevance, _ = _relevance(search_text, concepts, extras, table)
        score = min(5, relevance) + 2 * sum(_has(_normal(search_text), t) for t in special)
        score += 5 if scope.upper() in item['applies_to'] else 0
        score += {'nih-application-sections': 6, 'nih-general-writing': 4, 'nih-rigor-reproducibility': 5,
                  'nih-simplified-research-review': 6, 'nih-career-application-instructions': 6,
                  'nih-career-review-scope': 5, 'nih-k23-overview': 6}.get(item['id'], 0)
        candidates.append((score, item))
    result = []
    for _, item in sorted(candidates, key=lambda x: (-x[0], x[1]['id']))[:limit]:
        metadata = {key: item.get(key) for key in ('agency', 'checked_on', 'effective_from', 'source_version', 'source_sections',
                    'applies_to', 'not_applicable_to', 'content_type', 'caveats', 'tags') if key in item}
        metadata.update(guidance_id=item['id'], content_kind='curated_paraphrase', quoted_verbatim=False,
                        scope_requested=scope, timing_reference=reference_date.isoformat(),
                        timing_basis='proposal_due_date' if raw_due else 'current_date_due_date_unknown',
                        verification_required='Verify the active opportunity and policy notices before treating this catalog as a current requirement.')
        text = '\n'.join(['Guidance notes:', *('- ' + t for t in item['tips']), '', 'Scope and limitations:', *('- ' + t for t in item['caveats'])])
        result.append({'id': 'G_' + item['id'], 'layer': 'guidance', 'title': item['title'], 'text': text,
                       'source_uri': item['source_url'], 'metadata': metadata})
    return result, {'scope': scope, 'candidate_count': len(records), 'future_policy_ids_omitted': future,
                    'timing_reference': reference_date.isoformat()}


def _size(packet):
    return len(json.dumps(packet, ensure_ascii=False, separators=(',', ':')))


def _clip(source, limit):
    item = {**source, 'metadata': dict(source['metadata'])}
    original = source['text']
    end = min(len(original), max(0, limit))
    if end < len(original) and end > 100:
        boundary = original.rfind(' ', max(0, end - 200), end)
        if boundary > 0:
            end = boundary
    item['text'] = original[:end]
    item['metadata'].update(original_text_chars=len(original), included_text_chars=end,
                            truncated=end < len(original), text_range={'start': 0, 'end': end, 'unit': 'characters_zero_based_end_exclusive'})
    return item


def build_packet(db_path, brief: dict, *, max_personal=36, max_public=8, max_guidance=8, max_chars=65000) -> dict:
    """Return local evidence for a proposal, bounded by compact JSON characters.

    Sources retain independent IDs. A truncated source is an exact prefix, with
    its original length and character range explicit in metadata. `max_chars`
    covers the entire packet serialized with ensure_ascii=False and compact JSON
    separators, not only the source text. No model or network call is made.
    """
    if not isinstance(brief, dict):
        raise ValueError('brief must be an object')
    for name, value, ceiling in [('max_personal', max_personal, 100), ('max_public', max_public, 30), ('max_guidance', max_guidance, 30)]:
        if type(value) is not int or not 0 <= value <= ceiling:
            raise ValueError(f'{name} must be an integer from 0 to {ceiling}')
    if type(max_chars) is not int or not 4096 <= max_chars <= 1000000:
        raise ValueError('max_chars must be between 4096 and 1000000')
    projects = _strings(brief, 'projects', 40)
    table = _concept_table(db_path)
    concepts, extras, queries = _plan(brief, table)
    personal, p_coverage = _personal(db_path, queries, projects, concepts, extras, max_personal, table)
    public, n_coverage = _public(db_path, queries, concepts, extras, max_public, table)
    official, g_coverage = _official(brief, concepts, extras, max_guidance, table)
    with closing(_read_connection(db_path)) as conn:
        counts = dict(conn.execute('SELECT key,value FROM metadata'))
        document_count = conn.execute('SELECT count(*) FROM documents').fetchone()[0]
        project_rows = conn.execute('SELECT project,count(*) n,sum(unit_count>0) searchable FROM documents GROUP BY project').fetchall()
    warnings = [
        'Automatic labels and lexical matches are retrieval aids, not scientific verification. Inspect ownership, tense, model and source context.',
        'Configured aliases expand retrieval only. Original wording is preserved; tense and negation in non-English sources may not be captured by automatic evidence labels.',
        'Public project descriptions are proposed/funded work, not empirical findings. Guidance is a dated local catalog, not an opportunity-specific compliance determination.',
    ]
    if not personal:
        warnings.append('No personal source passages matched this brief and selected folder scope; do not substitute invented preliminary data.')
    if not queries:
        warnings.append('No scientific query terms were identified; add a research question or keywords to improve retrieval.')
    if not official and max_guidance:
        warnings.append('No applicable official guidance was retrieved for the supplied mechanism/funder; verify the actual opportunity.')
    if not brief.get('opportunity_url'):
        warnings.append('No funding-opportunity URL is recorded; opportunity-specific eligibility, instructions and review criteria remain unresolved.')
    known = {row['project'] for row in project_rows}
    unknown_projects = [p for p in projects if p not in known]
    if unknown_projects:
        warnings.append('Requested folders absent from the index: ' + ', '.join(unknown_projects))
    packet = {
        'version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'query_plan': {'method': 'local_multiquery_lexical_with_configured_aliases_and_source_diversity',
                       'concepts': concepts, 'other_terms': extras, 'queries': [{k: q[k] for k in ('label', 'reason')} for q in queries],
                       'projects': projects, 'role_scope': 'all_with_original_labels', 'network_requests': 0},
        'coverage': {'library_built_at': counts.get('built_at'), 'documents_in_index': document_count,
                     'requested_project_count': len(projects), 'unknown_projects': unknown_projects,
                     'personal': p_coverage, 'public_project': n_coverage, 'guidance': g_coverage,
                     'max_chars': max_chars, 'budget_basis': 'compact_json_characters', 'sources_omitted_for_budget': 0,
                     'selected_counts': {}, 'truncated_source_ids': []},
        'sources': [], 'warnings': warnings,
    }
    # Detailed per-query counts can dominate small packets; preserve summaries.
    if _size(packet) > max_chars // 3:
        packet['coverage']['personal']['matched_query_count'] = sum(x['candidate_count'] > 0 for x in p_coverage['matched_queries'])
        packet['coverage']['personal'].pop('matched_queries', None)
        packet['query_plan']['queries'] = packet['query_plan']['queries'][:8]
        packet['query_plan']['query_count'] = len(queries)
        packet['query_plan']['query_list_condensed'] = len(queries) > 8
    queues = [personal[:], official[:], public[:]]
    order = []
    while any(queues):
        for index in (0, 1, 2, 0, 0):
            if queues[index]:
                order.append(queues[index].pop(0))
    omitted = 0
    for source in order:
        cap = 2400 if source['layer'] == 'personal' else 3500 if source['layer'] == 'public_project' else 2200
        item = _clip(source, cap)
        empty = _clip(source, 0)
        room = max_chars - _size(packet) - _size(empty) - 450
        if room < 120:
            omitted += 1; continue
        if len(item['text']) > room:
            item = _clip(source, room)
        packet['sources'].append(item)
        if _size(packet) > max_chars - 350:
            packet['sources'].pop(); omitted += 1
    packet['coverage']['sources_omitted_for_budget'] = omitted
    packet['coverage']['selected_counts'] = dict(Counter(s['layer'] for s in packet['sources']))
    packet['coverage']['truncated_source_ids'] = [s['id'] for s in packet['sources'] if s['metadata']['truncated']]
    if omitted or packet['coverage']['truncated_source_ids']:
        packet['warnings'].append('Packet size limits omitted or shortened sources. Truncated text remains a verbatim prefix; consult the full source before relying on its context.')
    # Final bookkeeping is also inside the hard budget, including pathological
    # long metadata from source documents. Removing sources is explicit.
    while _size(packet) > max_chars and packet['sources']:
        packet['sources'].pop()
        packet['coverage']['sources_omitted_for_budget'] += 1
        packet['coverage']['selected_counts'] = dict(Counter(s['layer'] for s in packet['sources']))
        packet['coverage']['truncated_source_ids'] = [s['id'] for s in packet['sources'] if s['metadata']['truncated']]
    if _size(packet) > max_chars:
        raise ValueError('Brief metadata exceeds the context budget; shorten keywords/project names or increase max_chars')
    return packet
