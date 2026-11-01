"""Reference formatting without a citation-processor dependency.

Retained reference snapshots are converted to CSL-JSON (the Citation Style
Language item model used by Zotero, Mendeley and Pandoc) and rendered with a
small set of built-in journal and funder styles. Rendering is deterministic
text formatting of retained metadata; it does not verify a reference, judge
claim support, or fetch anything. Drafts always cite by stable source ID; the
style decides only how that ID appears in the exported text and reference list.
"""
from __future__ import annotations

import json
import re


CITATION = re.compile(r'\[\[([A-Za-z0-9_-]+)\]\]')
_ADJACENT = re.compile(r'\[\[[A-Za-z0-9_-]+\]\](?:[ ,;]*\[\[[A-Za-z0-9_-]+\]\])*')
_ADJACENT_ATTACHED = re.compile(r'[ \t]*\[\[[A-Za-z0-9_-]+\]\](?:[ ,;]*\[\[[A-Za-z0-9_-]+\]\])*')
_PARTICLES = {'van', 'von', 'de', 'der', 'den', 'da', 'del', 'della', 'di', 'du', 'la', 'le', 'dos', 'das', 'ter', 'ten'}
_COLLECTIVE = re.compile(r'\b(group|consortium|collaborat\w*|investigators|committee|network|team|study|initiative|'
                         r'alliance|society|association|working|programme|program|project)\b', re.I)

STYLES = {
    'vancouver': {'label': 'Vancouver / NLM (ICMJE; the usual choice for NIH applications)',
                  'citation': 'numeric', 'order': 'appearance', 'max_authors': 6, 'keep_authors': 6},
    'nature': {'label': 'Nature journals', 'citation': 'superscript', 'order': 'appearance',
               'max_authors': 5, 'keep_authors': 1},
    'science': {'label': 'Science (numbers shown in brackets; Science prints them in italic parentheses)',
                'citation': 'numeric', 'order': 'appearance', 'max_authors': 5, 'keep_authors': 1},
    'cell': {'label': 'Cell Press', 'citation': 'author-year', 'order': 'alphabetical',
             'max_authors': 10, 'keep_authors': 10},
    'nejm': {'label': 'New England Journal of Medicine', 'citation': 'numeric', 'order': 'appearance',
             'max_authors': 6, 'keep_authors': 3},
    'jama': {'label': 'JAMA / AMA Manual of Style', 'citation': 'superscript', 'order': 'appearance',
             'max_authors': 6, 'keep_authors': 3},
    'apa': {'label': 'APA 7th edition', 'citation': 'author-year', 'order': 'alphabetical',
            'max_authors': 20, 'keep_authors': 19},
}
DEFAULT_STYLE = 'vancouver'


def style_names():
    return list(STYLES)


def describe_styles():
    return [{'style': name, 'label': spec['label'], 'in_text': spec['citation'], 'list_order': spec['order']}
            for name, spec in STYLES.items()]


def check_style(style):
    if style not in STYLES:
        raise ValueError('Choose a reference style from: ' + ', '.join(STYLES) + '.')
    return style


# ---------------------------------------------------------------- CSL items

def _clean(value):
    return ' '.join(str(value).split()) if isinstance(value, (str, int)) and str(value).strip() else None


def parse_name(name):
    """Best-effort split of a plain author string; collective names stay literal."""
    name = _clean(name)
    if not name:
        return None
    if ',' in name:
        family, given = (part.strip() for part in name.split(',', 1))
        return {'family': family, 'given': given or None}
    words = name.split()
    if len(words) == 1 or len(words) > 4 or _COLLECTIVE.search(name):
        return {'literal': name}
    family = [words[-1]]
    rest = words[:-1]
    while rest and rest[-1].lower() in _PARTICLES:
        family.insert(0, rest.pop())
    return {'family': ' '.join(family), 'given': ' '.join(rest) or None}


def _names(metadata):
    parts = metadata.get('author_parts')
    if isinstance(parts, list) and parts and all(isinstance(p, dict) for p in parts):
        names = []
        for part in parts:
            if part.get('literal'):
                names.append({'literal': _clean(part['literal'])})
            elif part.get('family'):
                entry = {'family': _clean(part['family']), 'given': _clean(part.get('given'))}
                if part.get('suffix'):
                    entry['suffix'] = _clean(part['suffix'])
                names.append(entry)
        if names:
            return names
    return [n for n in (parse_name(a) for a in metadata.get('authors') or []) if n]


def csl_item(metadata, item_id):
    """Build one CSL-JSON item from a reference snapshot or a publication source's metadata."""
    canonical = metadata.get('canonical') or metadata.get('identifiers') or {}
    year = metadata.get('year')
    item = {'id': item_id,
            'type': 'book' if (metadata.get('work_type') or '').startswith('book') else 'article-journal',
            'title': _clean(metadata.get('title')),
            'author': _names(metadata),
            'container-title': _clean(metadata.get('journal')),
            'container-title-short': _clean(metadata.get('journal_abbreviation')),
            'volume': _clean(metadata.get('volume')), 'issue': _clean(metadata.get('issue')),
            'page': _clean(metadata.get('pages')),
            'issued': {'date-parts': [[int(year)]]} if isinstance(year, int) or (isinstance(year, str) and year.isdigit()) else None,
            'DOI': _clean(canonical.get('doi')), 'PMID': _clean(canonical.get('pmid')),
            'PMCID': _clean(canonical.get('pmcid')), 'URL': _clean(metadata.get('source_url') or metadata.get('source_uri')),
            'abstract': metadata.get('abstract') if isinstance(metadata.get('abstract'), str) else None}
    return {k: v for k, v in item.items() if v not in (None, [], {})}


def csl_json(items):
    return json.dumps(items, ensure_ascii=False, indent=1) + '\n'


# ---------------------------------------------------------------- RIS

def ris(items):
    """RIS records for EndNote, Zotero and Mendeley import; one TY/ER block per item."""
    out = []
    for item in items:
        lines = ['TY  - ' + ('BOOK' if item.get('type') == 'book' else 'JOUR')]
        for name in item.get('author', []):
            if name.get('literal'):
                lines.append('AU  - ' + name['literal'])
            else:
                lines.append('AU  - ' + name['family'] + (', ' + name['given'] if name.get('given') else '')
                             + (', ' + name['suffix'] if name.get('suffix') else ''))
        if item.get('title'):
            lines.append('TI  - ' + item['title'])
        if item.get('container-title'):
            lines.append('T2  - ' + item['container-title'])
        if item.get('container-title-short'):
            lines.append('JA  - ' + item['container-title-short'])
        year = _year(item)
        if year:
            lines.append(f'PY  - {year}')
        if item.get('volume'):
            lines.append('VL  - ' + item['volume'])
        if item.get('issue'):
            lines.append('IS  - ' + item['issue'])
        if item.get('page'):
            start, _, end = item['page'].partition('-')
            lines.append('SP  - ' + start.strip())
            if end.strip():
                lines.append('EP  - ' + end.strip())
        if item.get('DOI'):
            lines.append('DO  - ' + item['DOI'])
        if item.get('PMID'):
            lines.append('AN  - ' + item['PMID'])
            lines.append('UR  - https://pubmed.ncbi.nlm.nih.gov/' + item['PMID'] + '/')
        elif item.get('URL'):
            lines.append('UR  - ' + item['URL'])
        if item.get('abstract'):
            lines.append('AB  - ' + ' '.join(item['abstract'].split()))
        lines.append('ID  - ' + item['id'])
        lines.append('ER  - ')
        out.append('\n'.join(lines))
    return '\n'.join(out) + '\n'


# ---------------------------------------------------------------- rendering

def _year(item):
    parts = (item.get('issued') or {}).get('date-parts') or [[]]
    return parts[0][0] if parts and parts[0] else None


def _initials(given, *, dots, space):
    if not given:
        return ''
    tokens = []
    for word in re.split(r'[\s]+', given.strip()):
        if not word:
            continue
        hyphenated = [p[0].upper() + ('.' if dots else '') for p in word.split('-') if p]
        tokens.append('-'.join(hyphenated) if dots else ''.join(hyphenated))
    return (' ' if space else '').join(tokens)


def _name_nlm(name):            # Smith JP
    if name.get('literal'):
        return name['literal']
    return (name['family'] + ' ' + _initials(name.get('given'), dots=False, space=False)).strip()


def _name_family_initials(name, *, dots=True, space=True):   # Smith, J. P.
    if name.get('literal'):
        return name['literal']
    initials = _initials(name.get('given'), dots=dots, space=space)
    return name['family'] + (', ' + initials if initials else '')


def _name_initials_family(name):   # J. P. Smith
    if name.get('literal'):
        return name['literal']
    initials = _initials(name.get('given'), dots=True, space=True)
    return (initials + ' ' + name['family']).strip()


def _join(names, *, last_sep, et_al, max_authors, keep_authors, sep=', '):
    if len(names) > max_authors:
        return sep.join(names[:keep_authors]) + et_al
    if len(names) <= 1:
        return ''.join(names)
    return sep.join(names[:-1]) + last_sep + names[-1]


def _journal(item, *, abbreviated):
    if abbreviated:
        return item.get('container-title-short') or item.get('container-title') or ''
    return item.get('container-title') or item.get('container-title-short') or ''


def _title(item):
    return (item.get('title') or 'Untitled').rstrip('.?!') + ('?' if (item.get('title') or '').endswith('?') else '')


def _doi_url(item):
    return 'https://doi.org/' + item['DOI'] if item.get('DOI') else ''


def _dash(pages):
    return pages.replace('-', '–') if pages else ''


def format_entry(item, style):
    """One reference-list entry in Markdown (italic/bold markers only)."""
    spec = STYLES[check_style(style)]
    names, year = item.get('author', []), _year(item)
    year_text = str(year) if year else 'n.d.'
    vol, issue, pages = item.get('volume'), item.get('issue'), item.get('page')
    if style in ('vancouver', 'nejm', 'jama'):
        authors = _join([_name_nlm(n) for n in names], last_sep=', ', et_al=', et al',
                        max_authors=spec['max_authors'], keep_authors=spec['keep_authors'])
        journal = _journal(item, abbreviated=True)
        if style == 'vancouver':
            where = f'{year_text}' + (f';{vol}' if vol else '') + (f'({issue})' if issue else '') + (f':{pages}' if pages else '')
            parts = [authors, _title(item), journal, where]
            tail = (' doi:' + item['DOI'] + '.' if item.get('DOI') else '') + (' PMID: ' + item['PMID'] + '.' if item.get('PMID') else '')
            body = '. '.join(part for part in parts if part)
            return body + ('' if body.endswith('.') else '.') + tail
        if style == 'nejm':
            where = f'{year_text}' + (f';{vol}' if vol else '') + (f':{pages}' if pages else '')
            return f'{authors}. {_title(item)}. {journal} {where}.'
        where = f'{year_text}' + (f';{vol}' if vol else '') + (f'({issue})' if issue else '') + (f':{pages}' if pages else '')
        tail = ' doi:' + item['DOI'] if item.get('DOI') else ''
        return f'{authors}. {_title(item)}. *{journal}*. {where}.{tail}'
    if style == 'nature':
        authors = _join([_name_family_initials(n) for n in names], last_sep=' & ', et_al=' et al.',
                        max_authors=spec['max_authors'], keep_authors=spec['keep_authors'])
        journal = _journal(item, abbreviated=True)
        where = (f' **{vol}**' if vol else '') + (f', {_dash(pages)}' if pages else '')
        if authors and not authors.endswith('.'):
            authors += '.'
        return f'{authors} {_title(item)}. *{journal}*{where} ({year_text}).'.lstrip()
    if style == 'science':
        authors = _join([_name_initials_family(n) for n in names], last_sep=', ', et_al=' et al.',
                        max_authors=spec['max_authors'], keep_authors=spec['keep_authors'])
        journal = _journal(item, abbreviated=True)
        where = (f' **{vol}**' if vol else '') + (f', {_dash(pages)}' if pages else '')
        return f'{authors}, {_title(item)}. *{journal}*{where} ({year_text}).'
    if style == 'cell':
        authors = _join([_name_family_initials(n, dots=True, space=False) for n in names], last_sep=', and ',
                        et_al=', et al.', max_authors=spec['max_authors'], keep_authors=spec['keep_authors'])
        journal = _journal(item, abbreviated=True)
        where = (f' *{vol}*' if vol else '') + (f', {_dash(pages)}' if pages else '')
        doi = ' ' + _doi_url(item) + '.' if item.get('DOI') else ''
        return f'{authors} ({year_text}). {_title(item)}. *{journal}*{where}.{doi}'
    # APA 7
    authors = _join([_name_family_initials(n) for n in names], last_sep=', & ', et_al=', … ' + (_name_family_initials(names[-1]) if names else ''),
                    max_authors=spec['max_authors'], keep_authors=spec['keep_authors'])
    journal = _journal(item, abbreviated=False)
    where = (f', *{vol}*' if vol else '') + (f'({issue})' if issue else '') + (f', {_dash(pages)}' if pages else '')
    doi = ' ' + _doi_url(item) if item.get('DOI') else ''
    return f'{authors} ({year_text}). {_title(item)}. *{journal}*{where}.{doi}'


def _author_year_label(item, suffix=''):
    names, year = item.get('author', []), _year(item)
    year_text = (str(year) if year else 'n.d.') + suffix
    def family(name):
        return name.get('literal') or name.get('family') or 'Anonymous'
    if not names:
        return f'{_title(item)[:40]}, {year_text}'
    if len(names) == 1:
        return f'{family(names[0])}, {year_text}'
    if len(names) == 2:
        return f'{family(names[0])} and {family(names[1])}, {year_text}'
    return f'{family(names[0])} et al., {year_text}'


def _ranges(numbers):
    """1,2,3,5 -> '1–3,5' for superscript citation groups."""
    parts, start, previous = [], None, None
    for number in list(numbers) + [None]:
        if start is None:
            start = previous = number
            continue
        if number is not None and number == previous + 1:
            previous = number
            continue
        parts.append(str(start) if start == previous else f'{start},{previous}' if previous == start + 1 else f'{start}–{previous}')
        start = previous = number
    return ','.join(parts)


def in_text_preview(item, style):
    """How one item appears in the draft text under a style (numbered styles show the marker form)."""
    spec = STYLES[check_style(style)]
    if spec['citation'] == 'author-year':
        return '(' + _author_year_label(item) + ')'
    return 'superscript number, e.g. ¹' if spec['citation'] == 'superscript' else 'bracketed number, e.g. [1]'


def _sort_key(item):
    names = item.get('author', [])
    first = (names[0].get('literal') or names[0].get('family') or '') if names else (item.get('title') or '')
    return (first.lower(), _year(item) or 0, (item.get('title') or '').lower())


# ---------------------------------------------------------------- draft substitution

class Bibliography:
    """Resolve the citation markers of one draft against its retained sources.

    sources maps source ID -> source record ({'layer', 'title', 'metadata', ...}).
    references is the proposal's retained reference list; it supplies volume,
    pages and structured authors for full-text sources that lack them.
    """
    def __init__(self, text, sources, references=(), style=DEFAULT_STYLE):
        self.style = check_style(style)
        self.spec = STYLES[self.style]
        self.sources = sources
        self.used = list(dict.fromkeys(CITATION.findall(text)))
        self.numbers = {source_id: index + 1 for index, source_id in enumerate(self.used)}
        by_identity = {}
        for reference in references:
            canonical = reference.get('canonical') or reference.get('identifiers') or {}
            for kind in ('pmid', 'doi'):
                if canonical.get(kind):
                    by_identity.setdefault((kind, str(canonical[kind]).lower()), reference)
        self.items = {}
        for source_id in self.used:
            source = sources.get(source_id)
            if not source or source.get('layer') != 'publication':
                continue
            metadata = dict(source.get('metadata') or {})
            canonical = metadata.get('canonical') or {}
            match = next((by_identity[(k, str(canonical[k]).lower())] for k in ('pmid', 'doi')
                          if canonical.get(k) and (k, str(canonical[k]).lower()) in by_identity), None)
            if match:
                for key in ('author_parts', 'authors', 'journal', 'journal_abbreviation', 'year', 'volume', 'issue', 'pages', 'work_type'):
                    if not metadata.get(key) and match.get(key):
                        metadata[key] = match[key]
                if not metadata.get('abstract') and isinstance(match.get('abstract'), str):
                    metadata['abstract'] = match['abstract']
            metadata.setdefault('title', source.get('title'))
            metadata.setdefault('source_uri', source.get('source_uri'))
            self.items[source_id] = csl_item(metadata, source_id)
        self.labels = self._labels() if self.spec['citation'] == 'author-year' else {}

    def _labels(self):
        ordered = sorted(self.items.items(), key=lambda pair: _sort_key(pair[1]))
        groups = {}
        for source_id, item in ordered:
            groups.setdefault(_author_year_label(item), []).append(source_id)
        labels = {}
        for label, members in groups.items():
            if len(members) == 1:
                labels[members[0]] = label
                continue
            for index, source_id in enumerate(members):
                suffix = chr(ord('a') + index) if index < 26 else str(index)
                labels[source_id] = _author_year_label(self.items[source_id], suffix)
        return labels

    def marker(self, source_id):
        """In-text form of one marker (numeric styles keep the [n] form for Word anchoring)."""
        if source_id in self.labels:
            return self.labels[source_id]
        return f'[{self.numbers[source_id]}]'

    def substitute(self, text):
        if self.spec['citation'] == 'numeric':
            return CITATION.sub(lambda m: self.marker(m[1]), text)
        if self.spec['citation'] == 'superscript':
            # Superscript numerals attach to the preceding word; adjacent markers
            # merge into one ascending, range-compressed group such as [1–3,5].
            def group(match):
                numbers = sorted({self.numbers[i] for i in CITATION.findall(match[0])})
                return '[' + _ranges(numbers) + ']'
            return _ADJACENT_ATTACHED.sub(group, text)
        def group(match):
            ids = CITATION.findall(match[0])
            cited, numeric = [], []
            for source_id in dict.fromkeys(ids):
                (cited if source_id in self.labels else numeric).append(source_id)
            parts = []
            if cited:
                parts.append('(' + '; '.join(self.labels[i] for i in cited) + ')')
            parts.extend(f'[{self.numbers[i]}]' for i in numeric)
            return ''.join(parts)
        return _ADJACENT.sub(group, text)

    def year_suffix(self, source_id):
        label = self.labels.get(source_id, '')
        match = re.search(r'\d{4}([a-z])$', label)
        return match[1] if match else ''

    def entries(self):
        """Reference-list entries as (number, source_id, markdown) in list order."""
        if self.spec['order'] == 'alphabetical':
            publications = sorted(self.items, key=lambda i: _sort_key(self.items[i]))
            others = [i for i in self.used if i not in self.items]
            order = publications + others
        else:
            order = list(self.used)
        rows = []
        for source_id in order:
            if source_id in self.items:
                text = format_entry(self.items[source_id], self.style)
                suffix = self.year_suffix(source_id)
                if suffix:
                    text = re.sub(r'(\(\d{4})\)', r'\1' + suffix + ')', text, count=1) if re.search(r'\(\d{4}\)', text) else text
            else:
                source = self.sources.get(source_id)
                if source is None:
                    text = f'Unresolved source {source_id}; see the source notes.'
                else:
                    locator = (source.get('metadata') or {}).get('locator') or source.get('layer', 'source')
                    text = f"{source['title']} — {locator} (local source {source_id}; details in the source notes)."
                if self.spec['order'] == 'alphabetical':
                    text = f'[{self.numbers[source_id]}] {text}'
            rows.append((self.numbers[source_id], source_id, text))
        return rows

    def markdown(self):
        """The References section for an export; empty when nothing is cited."""
        rows = self.entries()
        if not rows:
            return ''
        lines = ['## References', '',
                 f'Style: {self.spec["label"]}. Entries are formatted from retained bibliographic metadata and have not been independently verified.', '']
        if self.spec['order'] == 'alphabetical':
            return '\n'.join(lines) + '\n' + '\n\n'.join(text for _number, _source_id, text in rows) + '\n'
        lines += [f'{number}. {text}' for number, _source_id, text in rows]
        return '\n'.join(lines) + '\n'

    def csl_items(self):
        return [self.items[i] for i in self.used if i in self.items]
