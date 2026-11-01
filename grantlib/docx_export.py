"""Editable Word export without an Office or Python package dependency.

The canonical Markdown export supplies the draft and its retained evidence notes.
This writer converts a bounded Markdown subset to editable WordprocessingML; it
never fetches images, links, source documents, or the current archive. Formatting
is a generic reading/editing layout, not a claim of funder compliance.
"""
from __future__ import annotations

from io import BytesIO
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


W = 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
XML = 'http://www.w3.org/XML/1998/namespace'
ET.register_namespace('w', W)
ET.register_namespace('r', R)
BODY_WIDTH = 9360  # Letter portrait, one-inch margins.
_INVALID_XML = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]')
_INLINE = re.compile(r'\[([^\]\n]+)\]\((https?://(?:[^\s()]|\([^()\s]*\))+)\)|\*\*([^*\n]+)\*\*|(?<!\*)\*([^*\n]+)\*(?!\*)|`([^`\n]+)`|\[(\d+(?:[,–]\d+)*)\]|https?://[^\s<>]+')
_LIST = re.compile(r'^( *)(?:([-+*])\s+|(\d+)[.)]\s+)(.+)$')
_HEADING = re.compile(r'^\s*(#{1,6})\s+(.+?)\s*#*\s*$')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')


def _w(tag, **attrs):
    return ET.Element('{' + W + '}' + tag, {'{' + W + '}' + k: str(v) for k, v in attrs.items()})


def _child(parent, tag, **attrs):
    node = _w(tag, **attrs)
    parent.append(node)
    return node


def _xml(node):
    return ET.tostring(node, encoding='utf-8', xml_declaration=True)


def _safe_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username or parsed.password or any(ord(c) < 33 for c in value)):
            return None
    except ValueError:
        return None
    return value


def _table_cells(line):
    text = line.strip()
    if text.startswith('|'):
        text = text[1:]
    if text.endswith('|') and not text.endswith('\\|'):
        text = text[:-1]
    cells, current, code = [], [], False
    i = 0
    while i < len(text):
        c = text[i]
        if c == '\\' and i + 1 < len(text) and text[i + 1] == '|':
            current.append('|')
            i += 2
            continue
        if c == '`':
            code = not code
        if c == '|' and not code:
            cells.append(''.join(current).strip())
            current = []
        else:
            current.append(c)
        i += 1
    cells.append(''.join(current).strip())
    return cells


def _is_divider(line):
    cells = _table_cells(line)
    return len(cells) > 1 and all(re.fullmatch(r':?-{3,}:?', c) for c in cells)


def _fenced_block(lines, index):
    delimiter = _FENCE.match(lines[index])[1]
    closing = re.compile(r'^ {0,3}' + re.escape(delimiter[0]) + '{' + str(len(delimiter)) + r',}\s*$')
    index += 1
    values = []
    while index < len(lines):
        if closing.fullmatch(lines[index]):
            return values, index + 1
        values.append(lines[index])
        index += 1
    raise ValueError('Close the Markdown code fence before exporting to Word.')


def _without_code(markdown):
    lines, values, index = markdown.split('\n'), [], 0
    while index < len(lines):
        if _FENCE.match(lines[index]):
            _, index = _fenced_block(lines, index)
        else:
            values.append(lines[index])
            index += 1
    return re.sub(r'`[^`\n]*`', '', '\n'.join(values))


def _literal_caption_pairs(markdown):
    """Read exact caption/quote pairs from generated baseline notes only."""
    lines, pairs, index, caption = markdown.replace('\r\n', '\n').split('\n'), set(), 0, None
    while index < len(lines):
        if _FENCE.match(lines[index]):
            values, index = _fenced_block(lines, index)
            if caption is not None:
                pairs.add((caption, '\n'.join(values)))
            caption = None
        else:
            if lines[index].strip():
                caption = lines[index]
            index += 1
    return pairs


class _Document:
    def __init__(self, citation_numbers, literal_blocks=None, *, superscript=False):
        self.superscript = superscript
        self.in_appendix = False
        self.root = _w('document')
        self.body = _child(self.root, 'body')
        self.rels = ET.Element('Relationships', xmlns=REL)
        for ident, kind, target in [('styles', 'styles', 'styles.xml'), ('numbering', 'numbering', 'numbering.xml')]:
            ET.SubElement(self.rels, 'Relationship', Id=ident, Type=R + '/' + kind, Target=target)
        self.urls = {}
        self.numbers = set(citation_numbers)
        self.bookmarks = set()
        self.next_bookmark = 1
        self.numbering = _w('numbering')
        self.num_count = 0
        self.literal_blocks = literal_blocks or {}

    def run(self, parent, text, *, bold=False, italic=False, code=False, link=False, superscript=False):
        if not text:
            return
        run = _child(parent, 'r')
        if bold or italic or code or link or superscript:
            prop = _child(run, 'rPr')
            if superscript:
                _child(prop, 'vertAlign', val='superscript')
            if bold:
                _child(prop, 'b')
            if italic:
                _child(prop, 'i')
            if code:
                _child(prop, 'rFonts', ascii='Courier New', hAnsi='Courier New')
            if link:
                _child(prop, 'color', val='000000')
                _child(prop, 'u', val='single')
        parts = text.split('\n')
        for index, part in enumerate(parts):
            if index:
                _child(run, 'br')
            node = _child(run, 't')
            node.set('{' + XML + '}space', 'preserve')
            node.text = part

    def hyperlink(self, parent, text, *, anchor=None, url=None, bold=False):
        if url is not None:
            url = _safe_url(url)
            if url is None:
                self.run(parent, text, bold=bold)
                return
            if url not in self.urls:
                ident = 'link' + str(len(self.urls) + 1)
                self.urls[url] = ident
                ET.SubElement(self.rels, 'Relationship', Id=ident, Type=R + '/hyperlink', Target=url, TargetMode='External')
            node = _child(parent, 'hyperlink', history=1)
            node.set('{' + R + '}id', self.urls[url])
        else:
            node = _child(parent, 'hyperlink', anchor=anchor, history=1)
        self.run(node, text, bold=bold, link=True)

    def inline(self, parent, text, *, bold=False):
        cursor = 0
        for match in _INLINE.finditer(text):
            self.run(parent, text[cursor:match.start()], bold=bold)
            if match[1] is not None:
                self.hyperlink(parent, match[1], url=match[2], bold=bold)
            elif match[3] is not None:
                self.inline(parent, match[3], bold=True)
            elif match[4] is not None:
                self.run(parent, match[4], bold=bold, italic=True)
            elif match[5] is not None:
                self.run(parent, match[5], bold=bold, code=True)
            elif match[6] is not None:
                cited = [int(n) for n in re.split(r'[,–]', match[6])]
                if self.superscript and not self.in_appendix and all(n in self.numbers for n in cited):
                    # Nature/JAMA-style superscript numerals in the draft body only.
                    self.run(parent, match[6], bold=bold, superscript=True)
                elif len(cited) == 1 and cited[0] in self.numbers:
                    self.hyperlink(parent, match[0], anchor='Source_' + match[6], bold=bold)
                else:
                    self.run(parent, match[0], bold=bold)
            else:
                # Keep sentence punctuation out of automatically recognized URLs.
                url = match[0].rstrip('.,;:!?')
                while url.endswith(')') and url.count(')') > url.count('('):
                    url = url[:-1]
                self.hyperlink(parent, url, url=url, bold=bold)
                self.run(parent, match[0][len(url):], bold=bold)
            cursor = match.end()
        self.run(parent, text[cursor:], bold=bold)

    def paragraph(self, text='', *, style=None, parent=None, bold=False, page_break=False, bookmark=None, num=None, level=0, align=None, code=False, literal=False):
        p = _child(parent if parent is not None else self.body, 'p')
        prop = _child(p, 'pPr')
        if style:
            _child(prop, 'pStyle', val=style)
        if page_break:
            _child(prop, 'pageBreakBefore')
        if align:
            _child(prop, 'jc', val=align)
        if num:
            np = _child(prop, 'numPr')
            _child(np, 'ilvl', val=level)
            _child(np, 'numId', val=num)
        if bookmark:
            identity = self.next_bookmark
            self.next_bookmark += 1
            _child(p, 'bookmarkStart', id=identity, name=bookmark)
            self.bookmarks.add(bookmark)
        if code or literal:
            self.run(p, text, code=code)
        else:
            self.inline(p, text, bold=bold)
        if bookmark:
            _child(p, 'bookmarkEnd', id=identity)
        return p

    def new_list(self, ordered, start=1, level=0):
        self.num_count += 1
        # Independent definitions prevent a later restart from changing an earlier
        # list in Word-compatible renderers that merge shared abstract definitions.
        abstract = _w('abstractNum', abstractNumId=self.num_count)
        _child(abstract, 'nsid', val=format(self.num_count, '08X'))
        _child(abstract, 'multiLevelType', val='multilevel')
        for depth in range(9):
            lvl = _child(abstract, 'lvl', ilvl=depth)
            _child(lvl, 'start', val=start if depth == level and ordered else 1)
            _child(lvl, 'numFmt', val='decimal' if ordered else 'bullet')
            _child(lvl, 'lvlText', val='%' + str(depth + 1) + '.' if ordered else '•')
            _child(lvl, 'lvlJc', val='left')
            ppr = _child(lvl, 'pPr')
            _child(ppr, 'ind', left=720 + depth * 360, hanging=360)
        self.numbering.insert(self.num_count - 1, abstract)
        num = _child(self.numbering, 'num', numId=self.num_count)
        _child(num, 'abstractNumId', val=self.num_count)
        if ordered and start != 1:
            override = _child(num, 'lvlOverride', ilvl=level)
            _child(override, 'startOverride', val=start)
        return self.num_count

    def table(self, rows, separators):
        count = len(rows[0])
        if count > 8 or any(len(row) != count for row in rows):
            raise ValueError('Word export needs tables with a consistent number of columns (at most eight).')
        weights = [max(8, min(48, max(len(row[i]) for row in rows))) for i in range(count)]
        widths = [int(BODY_WIDTH * weight / sum(weights)) for weight in weights]
        widths[-1] += BODY_WIDTH - sum(widths)
        table = _child(self.body, 'tbl')
        prop = _child(table, 'tblPr')
        _child(prop, 'tblW', w=BODY_WIDTH, type='dxa')
        _child(prop, 'tblLayout', type='fixed')
        borders = _child(prop, 'tblBorders')
        for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
            _child(borders, edge, val='single', sz=4, color='D9D9D9')
        margins = _child(prop, 'tblCellMar')
        for edge, width in [('top', 100), ('left', 120), ('bottom', 100), ('right', 120)]:
            _child(margins, edge, w=width, type='dxa')
        grid = _child(table, 'tblGrid')
        for width in widths:
            _child(grid, 'gridCol', w=width)
        for index, row in enumerate(rows):
            tr = _child(table, 'tr')
            trpr = _child(tr, 'trPr')
            _child(trpr, 'cantSplit')
            if index == 0:
                _child(trpr, 'tblHeader')
            for col, value in enumerate(row):
                cell = _child(tr, 'tc')
                cp = _child(cell, 'tcPr')
                _child(cp, 'tcW', w=widths[col], type='dxa')
                _child(cp, 'vAlign', val='center')
                if index == 0:
                    _child(cp, 'shd', val='clear', color='auto', fill='E8EEF0')
                separator = separators[col]
                align = 'center' if separator.startswith(':') and separator.endswith(':') else 'right' if separator.endswith(':') else 'left'
                self.paragraph(value, style='TableText', parent=cell, bold=index == 0, align=align)
        self.paragraph('', style='TableAfter')

    def markdown(self, markdown, *, title=False, appendix=False, finding_introductions=(), anchor_captions=()):
        lines, i = markdown.replace('\r\n', '\n').split('\n'), 0
        first = True
        last_plain = None
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            previous_plain, last_plain = last_plain, None
            heading = _HEADING.match(line)
            if line in self.literal_blocks:
                self.paragraph(self.literal_blocks[line], literal=True)
                i += 1
            elif heading:
                self.paragraph(heading[2], style='Title' if title and first else 'Heading' + str(min(3, len(heading[1]))))
                i += 1
            elif re.fullmatch(r'\s*(?:---+|\*\*\*+)\s*', line):
                # Appendix headings already provide separation. Extra empty
                # paragraphs can strand a final review note on an otherwise blank page.
                if not appendix:
                    self.paragraph('')
                i += 1
            elif _FENCE.match(line):
                values, i = _fenced_block(lines, i)
                text = '\n'.join(values)
                if previous_plain and (previous_plain[0], text) in anchor_captions:
                    # Keep the exact saved caption with the quote's first lines,
                    # without preventing a long literal quote from splitting.
                    _child(previous_plain[1].find('{' + W + '}pPr'), 'keepNext')
                self.paragraph(text, style='Code', code=True)
            elif i + 1 < len(lines) and _is_divider(lines[i + 1]):
                rows, separators = [_table_cells(line)], _table_cells(lines[i + 1])
                i += 2
                while i < len(lines) and lines[i].strip() and '|' in lines[i]:
                    rows.append(_table_cells(lines[i]))
                    i += 1
                if len(separators) != len(rows[0]):
                    raise ValueError('The Markdown table header and divider have different column counts.')
                self.table(rows, separators)
            elif _LIST.match(line):
                numbered = bool(_LIST.match(line)[3])
                sequences = {}
                while i < len(lines):
                    item = _LIST.match(lines[i])
                    if not item or bool(item[3]) != numbered:
                        break
                    level, value = min(8, len(item[1]) // 2), int(item[3] or 1)
                    if level not in sequences or numbered and value != sequences[level][1]:
                        sequences[level] = (self.new_list(numbered, value, level), value)
                    num = sequences[level][0]
                    self.paragraph(item[4], style='ListParagraph', num=num, level=level)
                    sequences[level] = (num, value + 1)
                    sequences = {depth: seq for depth, seq in sequences.items() if depth <= level}
                    i += 1
            elif line.lstrip().startswith('>'):
                parts = []
                while i < len(lines) and lines[i].lstrip().startswith('>'):
                    parts.append(re.sub(r'^\s*>\s?', '', lines[i]))
                    i += 1
                self.paragraph('\n'.join(parts), style='Quote')
            else:
                parts = [line]
                i += 1
                while i < len(lines) and lines[i].strip():
                    other = lines[i]
                    if (_HEADING.match(other) or _LIST.match(other) or other.lstrip().startswith('>') or _FENCE.match(other)
                            or re.fullmatch(r'\s*---+\s*', other) or i + 1 < len(lines) and _is_divider(lines[i + 1])):
                        break
                    # Source entries and their metadata remain separate paragraphs.
                    if appendix or re.match(r'^\[\d+\]', other):
                        break
                    parts.append(other)
                    i += 1
                text = '\n'.join(parts)
                bookmark = None
                source = re.match(r'^\[(\d+)\]\s', text)
                if appendix and source and int(source[1]) in self.numbers:
                    bookmark = 'Source_' + source[1]
                style = 'Heading2' if appendix and text in ('Source notes for investigator review', 'Review notes') else None
                paragraph = self.paragraph(text, style=style, bookmark=bookmark)
                if previous_plain and (previous_plain[0], text) in finding_introductions:
                    # Keep only the saved finding title and assessment with the
                    # start of its explanation. Repairs and evidence stay free
                    # to flow, even when a finding is longer than a page.
                    for introduction in (previous_plain[1], paragraph):
                        _child(introduction.find('{' + W + '}pPr'), 'keepNext')
                last_plain = (text, paragraph)
            first = False


def _styles(language):
    styles = _w('styles')
    defaults = _child(styles, 'docDefaults')
    rp = _child(_child(defaults, 'rPrDefault'), 'rPr')
    _child(rp, 'rFonts', ascii='Arial', hAnsi='Arial', eastAsia='Arial', cs='Arial')
    _child(rp, 'sz', val=22)
    _child(rp, 'szCs', val=22)
    _child(rp, 'color', val='000000')
    _child(rp, 'lang', val=language)
    pp = _child(_child(defaults, 'pPrDefault'), 'pPr')
    _child(pp, 'spacing', after=100, line=252, lineRule='auto')
    _child(pp, 'widowControl')
    for ident, label, size, before, after in [
            ('Normal', 'Normal', 22, 0, 100), ('Title', 'Title', 30, 0, 180),
            ('Heading1', 'heading 1', 26, 180, 100), ('Heading2', 'heading 2', 24, 140, 80),
            ('Heading3', 'heading 3', 22, 100, 60), ('ListParagraph', 'List Paragraph', 22, 0, 60),
            ('TableText', 'Table Text', 22, 0, 0), ('TableAfter', 'Table After', 12, 0, 60),
            ('Quote', 'Quote', 22, 60, 100), ('Code', 'Code', 20, 60, 100)]:
        style = _child(styles, 'style', type='paragraph', styleId=ident)
        if ident == 'Normal':
            style.set('{' + W + '}default', '1')
        _child(style, 'name', val=label)
        if ident != 'Normal':
            _child(style, 'basedOn', val='Normal')
        _child(style, 'next', val='Normal')
        prop = _child(style, 'pPr')
        _child(prop, 'spacing', before=before, after=after, line=252, lineRule='auto')
        if ident.startswith('Heading') or ident == 'Title':
            _child(prop, 'keepNext')
            _child(prop, 'keepLines')
        if ident.startswith('Heading'):
            _child(prop, 'outlineLvl', val=int(ident[-1]) - 1)
        if ident in ('Quote', 'Code'):
            _child(prop, 'ind', left=360, right=360)
        run = _child(style, 'rPr')
        _child(run, 'sz', val=size)
        _child(run, 'color', val='000000')
        if ident.startswith('Heading') or ident == 'Title':
            _child(run, 'b')
    return _xml(styles)


def export_docx(proposal, artifact_id, style=None):
    """Return a DOCX containing one immutable draft and its review appendices.

    Source references use the selected artifact's retained snapshot. With a
    reference style, the formatted References section follows the draft body and
    numeric superscript styles render superscript numerals. Unsupported
    image/LaTeX notation is rejected explicitly rather than silently discarded or
    represented as an editable figure/equation. Callers choose where bytes go.
    """
    from .bibliography import STYLES, check_style
    from .studio import CITATION, REFERENCES_MARKER, _baseline_notes, cited_bibliography, export_markdown

    if style is not None:
        check_style(style)
    markdown = export_markdown(proposal, artifact_id, style=style)
    artifact = next(a for a in proposal['artifacts'] if a['id'] == artifact_id)
    if len(markdown) > 300000:
        raise ValueError('This Word export exceeds the supported text size.')
    if _INVALID_XML.search(markdown) or _INVALID_XML.search(artifact.get('title', '')):
        raise ValueError('The draft contains characters that cannot be represented in Word XML.')
    without_code = _without_code(artifact['markdown'])
    if re.search(r'!\[[^\]]*\]\(', without_code):
        raise ValueError('Word export does not embed Markdown images yet. Insert the figure in Word or export the text without the image reference.')
    if re.search(r'\\(?:\(|\[|begin\{)|\$\$|(?<![\\$])\$[^$\n]+\$(?!\d)', without_code):
        raise ValueError('Word export needs a supported editable equation before exporting LaTeX notation.')
    # The exact saved draft is the export prefix. Do not infer its boundary
    # from headings: commentary or a quoted example may repeat those headings.
    if style is None:
        body_numbers = {}
        def number_body(match):
            number = body_numbers.setdefault(match[1], len(body_numbers) + 1)
            return f'[{number}]'
        main = CITATION.sub(number_body, artifact['markdown'])
    else:
        main = cited_bibliography(proposal, artifact_id, style).substitute(artifact['markdown'])
    if not markdown.startswith(main):
        raise ValueError('The exported draft does not match its saved version.')
    appendix = markdown[len(main):]
    references_block = ''
    if style is not None:
        start = appendix.find(REFERENCES_MARKER)
        if start >= 0:
            end = appendix.find('\n\n---\n\n', start + len(REFERENCES_MARKER))
            end = len(appendix) if end < 0 else end
            references_block = appendix[start + len('\n\n---\n\n'):end]
            appendix = appendix[:start] + appendix[end:]
    # The canonical export has a source-notes appendix at the end.
    source_boundary = markdown.rfind('\n---\n\nSource notes for investigator review\n')
    if source_boundary < len(main):
        raise ValueError('The selected draft has no canonical source-note appendix.')
    source_numbers = [int(n) for n in re.findall(r'^\[(\d+)\]\s', markdown[source_boundary:], flags=re.M)]
    # Retained quotations may contain Markdown syntax. Protect the exact source
    # text before parsing the review appendix; render it as literal editable text.
    literal_blocks = {}
    for claim in artifact.get('claims', []):
        for assessment in claim.get('assessments', []):
            excerpt = assessment.get('excerpt')
            if not excerpt:
                continue
            fence = '`' * max(3, max((len(run) for run in re.findall(r'`+', excerpt)), default=0) + 1)
            needle = '  Read excerpt:\n\n' + fence + 'text\n' + excerpt + '\n' + fence + '\n\n  Locator:'
            if needle not in appendix:
                continue
            token = 'GRANT_DOCX_LITERAL_BLOCK_' + str(len(literal_blocks))
            while token in markdown or token in literal_blocks:
                token += '_'
            literal_blocks[token] = 'Read excerpt: ' + excerpt
            appendix = appendix.replace(needle, token + '\n  Locator:')
    document = _Document(source_numbers, literal_blocks,
                         superscript=style is not None and STYLES[style]['citation'] == 'superscript')
    first_line = next((line for line in main.splitlines() if line.strip()), '')
    if not _HEADING.match(first_line):
        document.paragraph(artifact.get('title') or 'Grant proposal', style='Title')
    document.markdown(main, title=True)
    if references_block:
        document.markdown(references_block)
    document.in_appendix = True
    document.paragraph('Investigator review notes', style='Heading1', page_break=True)
    finding_introductions, anchor_captions = set(), set()
    for key in ('revision_check', 'draft_check'):
        report = (artifact.get('model_metadata', {}).get(key) or {}).get('report') or {}
        for finding in report.get('findings', []):
            finding_introductions.add((finding['id'] + ': ' + finding['summary'],
                                       'Assessment: ' + finding['severity'] + '; ' + finding['resolution'] + '.'))
        for item in report.get('findings', []) + report.get('commitments', []):
            for field in ('before', 'after', 'authority', 'sources'):
                for anchor in item.get(field, []):
                    kind = ('initial candidate before repair'
                            if key == 'draft_check' and anchor['kind'] == 'repair_baseline' else anchor['kind'])
                    caption = f"{field.title()} — {kind} {anchor['id']} / {anchor['field']}"
                    anchor_captions.add((caption, anchor['quote'].replace('\r\n', '\n')))
    baseline = (artifact.get('model_metadata', {}).get('draft_check') or {}).get('repair_baseline')
    if baseline:
        sources = {source['id']: source for source in (artifact.get('context') or {}).get('sources', [])}
        anchor_captions.update(_literal_caption_pairs(_baseline_notes(baseline, sources)))
    document.markdown(appendix.lstrip('\n-'), appendix=True,
                      finding_introductions=finding_introductions, anchor_captions=anchor_captions)
    for number in source_numbers:
        if 'Source_' + str(number) not in document.bookmarks:
            raise ValueError('A source note could not be anchored in the Word export.')
    section = _child(document.body, 'sectPr')
    _child(section, 'pgSz', w=12240, h=15840)
    _child(section, 'pgMar', top=1440, right=1440, bottom=1440, left=1440, header=720, footer=720, gutter=0)
    lang = {'English': 'en-US', 'French': 'fr-FR', 'Spanish': 'es-ES', 'Italian': 'it-IT', 'German': 'de-DE'}.get(proposal.get('brief', {}).get('language'), 'en-US')
    types = ET.Element('Types', xmlns=CT)
    for ext, value in [('rels', 'application/vnd.openxmlformats-package.relationships+xml'), ('xml', 'application/xml')]:
        ET.SubElement(types, 'Default', Extension=ext, ContentType=value)
    for part, value in [('document', 'document.main'), ('styles', 'styles'), ('numbering', 'numbering')]:
        ET.SubElement(types, 'Override', PartName='/word/' + part + '.xml', ContentType='application/vnd.openxmlformats-officedocument.wordprocessingml.' + value + '+xml')
    root_rels = ET.Element('Relationships', xmlns=REL)
    ET.SubElement(root_rels, 'Relationship', Id='document', Type=R + '/officeDocument', Target='word/document.xml')
    members = {'[Content_Types].xml': _xml(types), '_rels/.rels': _xml(root_rels),
               'word/document.xml': _xml(document.root), 'word/styles.xml': _styles(lang),
               'word/numbering.xml': _xml(document.numbering), 'word/_rels/document.xml.rels': _xml(document.rels)}
    buffer = BytesIO()
    with ZipFile(buffer, 'w', compression=ZIP_DEFLATED) as archive:
        for name, data in members.items():
            entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            entry.compress_type = ZIP_DEFLATED
            archive.writestr(entry, data)
    return buffer.getvalue()
