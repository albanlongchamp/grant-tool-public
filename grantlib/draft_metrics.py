"""Deterministic local draft counts, independent of any model assessment.

Only supplied text is examined. No instructions, numeric caps, funder rules or
claims about shortening are inferred. These are not official Word/funder counts.
"""
from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
import unicodedata


METHOD_VERSION = 'draft-words-v1'
METHOD = ('Count whitespace-separated tokens containing a Unicode letter or digit after '
          'removing [[citation_ids]] and Markdown link destinations while retaining link labels. '
          'Markup-only tokens do not count; headings and hyphenated scientific values count consistently.')
_LIMITATIONS = [
    'This local convention is not an official funder, page-limit, or Microsoft Word count.',
    'Counts describe text length, not scientific quality, supported claims, or compliance with instructions.',
]
_CITATION = re.compile(r'\[\[[A-Za-z0-9_-]+\]\]')
_ATX = re.compile(r'^ {0,3}#{1,6}(?:[ \t]+(.*)|[ \t]*)$')
_BOLD = re.compile(r'^ {0,3}(\*\*|__)(.+?)\1[ \t]*$')
_BOLD_LEAD = re.compile(r'^ {0,3}(\*\*|__)(.+?)\1[ \t]+\S')
_FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')
_RULE = re.compile(r'^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$')
_SETEXT = re.compile(r'^ {0,3}(?:=+|-+)[ \t]*$')
_DEFINITION = re.compile(
    r'''^ {0,3}\[([^\]\n]+)\]:[ \t]*(?:<[^>\n]+>|\S+)(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^\)\n]*\)))?[ \t]*$''')
_CHANGE_NAMES = frozenset({
    'material changes', 'material changes from the selected draft',
    'material changes from the selected version', 'changes from the selected draft',
    'changes from the selected version', 'changes relative to the selected draft',
    'changes relative to the selected version', 'change summary', 'summary of changes',
})


def _hash(text):
    try:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()
    except UnicodeError:
        raise ValueError('Draft text must contain valid Unicode.') from None


def _name(value):
    return unicodedata.normalize('NFC', ' '.join(value.split())).casefold()


def _heading(line):
    match = _ATX.fullmatch(line)
    if match:
        value = match[1] or ''
        return re.sub(r'[ \t]+#+[ \t]*$', '', value).strip()
    match = _BOLD.fullmatch(line)
    return match[2].strip() if match else None


def _line_states(markdown):
    """Yield exact line offsets and whether a line is outside code/quotation."""
    offset, fence = 0, None
    for raw in markdown.splitlines(keepends=True):
        line = raw.rstrip('\r\n')
        match = _FENCE.match(line)
        quoted = bool(re.match(r'^ {0,3}>', line))
        if fence:
            if (match and match[1][0] == fence[0] and len(match[1]) >= fence[1]
                    and not match[2].strip()):
                fence = None
            yield offset, line, False
        elif quoted or line.startswith(('    ', '\t')):
            yield offset, line, False
        elif match:
            fence = (match[1][0], len(match[1]))
            yield offset, line, False
        else:
            yield offset, line, True
        offset += len(raw)


def _legacy_body(markdown):
    states = list(_line_states(markdown))
    candidates = [(i, offset, heading) for i, (offset, line, outside) in enumerate(states)
                  if outside and (heading := _heading(line)) is not None
                  and _name(heading) in _CHANGE_NAMES]
    if not candidates:
        return markdown, None, ['No explicit body field or narrowly recognized trailing change-summary heading; counted the full Markdown.']
    if len(candidates) != 1:
        return markdown, None, ['Multiple possible change-summary headings make the legacy boundary ambiguous; counted the full Markdown.']
    index, offset, heading = candidates[0]
    if not _count(markdown[:offset]):
        return markdown, None, ['The possible change summary has no preceding draft body; counted the full Markdown.']
    for _, line, outside in states[index + 1:]:
        if outside and (_heading(line) is not None or _BOLD_LEAD.match(line)
                        or _RULE.fullmatch(line) or _SETEXT.fullmatch(line)):
            return markdown, None, ['A later section heading or separator makes the legacy boundary ambiguous; counted the full Markdown.']
    # Restrict exclusion to an ordinary trailing section. Unclosed fences can
    # conceal later sections and are therefore not a reliable appendix boundary.
    trailing = markdown[offset:]
    opened = None
    for _, line, outside in _line_states(trailing):
        if re.match(r'^ {0,3}>', line):
            continue
        match = _FENCE.match(line)
        if match:
            if opened and match[1][0] == opened[0] and len(match[1]) >= opened[1] and not match[2].strip():
                opened = None
            elif opened is None:
                opened = (match[1][0], len(match[1]))
    if opened:
        return markdown, None, ['An unclosed fenced block makes the legacy boundary ambiguous; counted the full Markdown.']
    return markdown[:offset], heading, [
        'A legacy change appendix was excluded using a narrow heading convention, not semantic analysis; formatting can make the intended boundary uncertain.'
    ]


def _balanced(text, start, opening, closing):
    depth, index, quoted, angled = 1, start + 1, None, False
    # This is a bounded recognizer for ordinary single-line Markdown links,
    # not a Markdown parser. Malformed/long constructs remain in the count.
    while index < min(len(text), start + 8192):
        char = text[index]
        if char in '\r\n':
            return None
        if char == '\\':
            index += 2
            continue
        if opening == '(':
            if quoted:
                if char == quoted:
                    quoted = None
                index += 1
                continue
            if angled:
                if char == '>':
                    angled = False
                index += 1
                continue
            if char == '<':
                angled = True
                index += 1
                continue
            if char in ('"', "'") and (index == start + 1 or text[index - 1].isspace()):
                quoted = char
                index += 1
                continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return index
        index += 1
    return None


def _without_destinations(text):
    definitions, lines = set(), []
    for _, line, outside in _line_states(text):
        match = _DEFINITION.fullmatch(line) if outside else None
        if match:
            definitions.add(_name(match[1]))
        else:
            lines.append(line)
    text = '\n'.join(lines)
    result, index = [], 0
    while index < len(text):
        if text[index] != '[' or (index and text[index - 1] == '\\'):
            result.append(text[index])
            index += 1
            continue
        end = _balanced(text, index, '[', ']')
        if end is None:
            result.append(text[index:])
            break
        label = text[index + 1:end]
        after = end + 1
        if after < len(text) and text[after] == '(':
            destination_end = _balanced(text, after, '(', ')')
            if destination_end is not None:
                result.append(label)
                index = destination_end + 1
                continue
        if after < len(text) and text[after] == '[':
            reference_end = text.find(']', after + 1)
            if reference_end >= 0 and _name(text[after + 1:reference_end] or label) in definitions:
                result.append(label)
                index = reference_end + 1
                continue
        result.append(text[index:end + 1])
        index = end + 1
    return ''.join(result)


def _count(markdown):
    text = _without_destinations(_CITATION.sub('', markdown))
    return sum(any(c.isalpha() or c.isdigit() for c in token) for token in text.split())


def _explicit_summary(version):
    if 'change_summary' in version:
        value = version['change_summary']
    else:
        structured = version.get('structured')
        if isinstance(structured, str):
            try:
                structured = json.loads(structured)
            except (ValueError, RecursionError):
                raise ValueError('The stored structured response must be a valid JSON object.') from None
        if not isinstance(structured, Mapping) or 'change_summary' not in structured:
            return None
        value = structured['change_summary']
    if not isinstance(value, str):
        raise ValueError('An explicit change_summary must be text, including an empty string.')
    return value


def measure(version):
    """Measure a model output or saved artifact mapping without modifying it."""
    if not isinstance(version, Mapping) or not isinstance(version.get('markdown'), str):
        raise ValueError('A draft measurement requires a mapping with Markdown text.')
    markdown = version['markdown']
    input_hash = _hash(markdown)
    summary = _explicit_summary(version)
    if summary is not None:
        body, heading, scope = markdown, None, 'explicit_body'
        limitations = []
    else:
        body, heading, limitations = _legacy_body(markdown)
        scope = 'legacy_body_before_change_summary' if heading is not None else 'full_markdown'
    result = {'words': _count(body), 'full_markdown_words': _count(markdown),
              'scope': scope, 'excluded_heading': heading, 'input_sha256': input_hash,
              'body_sha256': _hash(body), 'hash_scope': 'exact_markdown_utf8',
              'method_version': METHOD_VERSION, 'method': METHOD,
              'limitations': [*_LIMITATIONS, *limitations]}
    if summary is not None:
        result['change_summary_sha256'] = _hash(summary)
    return result


def compare(parent, candidate):
    """Return a persistable comparison; no model verdict is consulted."""
    before, after = measure(parent), measure(candidate)
    delta = after['words'] - before['words']
    parent_id = parent.get('id')
    if parent_id is not None and not isinstance(parent_id, str):
        raise ValueError('The parent artifact identifier must be text or null.')
    return {'parent_artifact_id': parent_id, 'parent': before, 'candidate': after,
            'delta_words': delta,
            'percent_change': round(100 * delta / before['words'], 2) if before['words'] else None,
            'direction': 'shorter' if delta < 0 else 'longer' if delta > 0 else 'unchanged',
            'method_version': METHOD_VERSION, 'method': METHOD,
            'limitations': list(dict.fromkeys([*before['limitations'], *after['limitations']]))}


def export_markdown(report):
    """Render saved measurements, independently of any model-authored notes."""
    percent = report['percent_change']
    change = f"{report['delta_words']:+d} words"
    change += f' ({percent:+.2f}%)' if percent is not None else ' (percentage unavailable: parent count is zero)'
    lines = ['## Local draft length comparison', '',
             f"Parent: {report['parent']['words']} words. Candidate: {report['candidate']['words']} words. "
             f"Change: {change}; {report['direction']}.", '',
             f"Count convention ({report.get('method_version', METHOD_VERSION)}): {report['method']}", '',
             f"Scope: parent {report['parent']['scope']}; candidate {report['candidate']['scope']}."]
    if report.get('parent_artifact_id'):
        lines += ['', 'Parent artifact: ' + report['parent_artifact_id'] + '.']
    for label, item in [('Parent', report['parent']), ('Candidate', report['candidate'])]:
        if item.get('excluded_heading'):
            lines.append(label + ' legacy appendix excluded at heading: ' + item['excluded_heading'] + '.')
    if report.get('limitations'):
        lines += ['', 'Limitations:'] + ['- ' + value for value in report['limitations']]
    return '\n'.join(lines) + '\n'
