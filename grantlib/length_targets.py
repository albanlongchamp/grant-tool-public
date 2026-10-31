"""Explicit task word targets assessed using the versioned local count convention."""
import hashlib
import re
import unicodedata

from . import draft_metrics


RESOLVER_VERSION = 'task-word-request-v1'
_NUMBER = r'\d{1,5}'
_QUANTITY = (rf'(?:between\s+{_NUMBER}\s+and\s+{_NUMBER}|'
             rf'{_NUMBER}\s*(?:[-–—]|\bto\b)\s*{_NUMBER}|'
             rf'(?:at\s+most|no\s+more\s+than|up\s+to|at\s+least|no\s+fewer\s+than|'
             rf'fewer\s+than|less\s+than|under|below|exactly)\s+{_NUMBER}|{_NUMBER})')
_BODY = r'(?:(?:specific\s+aims\s+)?main\s+(?:draft|body|text)|draft\s+body|specific\s+aims(?:\s+draft)?|draft|body|text)'
_LEAD = r'^(?:please\s+)?'
_DIRECTIVES = [
    # Each pattern starts at an imperative or an explicitly labelled main body.
    # No free number search can make a passage into an operative constraint.
    (_LEAD + rf'(?:write|produce|draft|compose)\s+(?:(?:a|an|the)\s+)?(?P<amount>{_QUANTITY})\s*[-–]?\s*words?\s+{_BODY}\b', None, True),
    (_LEAD + rf'(?:write|produce|draft|compose)\s+(?:(?:a|an|the)\s+)?(?:coherent\s+)?{_BODY}\s+(?:of|in)\s+(?P<amount>{_QUANTITY})\s*[-–]?\s*words?\b', None, True),
    (_LEAD + rf'develop\s+[^\n.!?;:\d]{{1,180}}?\s+into\s+(?:(?:a|an|the)\s+)?(?:coherent\s+)?{_BODY}\s+(?:of|in)\s+(?P<amount>{_QUANTITY})\s*[-–]?\s*words?\b', None, True),
    (_LEAD + rf'(?:write|produce|draft|compose)\s+(?P<amount>{_QUANTITY})\s+words?\b', None, False),
    (_LEAD + rf'keep\s+(?:the\s+)?{_BODY}\s+(?:to\s+)?(?P<amount>{_QUANTITY})\s+words?\b', None, True),
    (_LEAD + rf'limit\s+(?:the\s+)?{_BODY}\s+to\s+(?P<amount>{_NUMBER})\s+words?\b', 'maximum', True),
    (_LEAD + rf'(?:do\s+not|don[\x27’]t)\s+exceed\s+(?P<amount>{_NUMBER})\s+words?\b', 'maximum', False),
    (rf'^main\s+(?:draft|body|text)\s*:\s*(?P<amount>{_QUANTITY})\s*[-–]?\s*words?\b', None, True),
]
_DIRECTIVES = [(re.compile(pattern, re.I), forced, body) for pattern, forced, body in _DIRECTIVES]
_SOFT = re.compile(r'\b(?:about|around|approximately|roughly)\b|[~≈]|\b(?:shorter|longer|shorten|reduce|cut|remove)\b|%', re.I)
_COMPONENT_NAMES = r'(?:aim\s+\d+|background|summary|abstract|methods?|results?|discussion|introduction|caption|legend)'
_COMPONENT = re.compile(r'\b(?:each|per)\s+(?:aim|section|paragraph)|\b(?:' + _COMPONENT_NAMES + r')\b', re.I)
_WORD_CUE = re.compile(r'\d[^\n.!?;]{0,45}\bwords?\b|\bword\s+(?:limit|target)\b', re.I)
_REQUEST = re.compile(r'^(?:please\s+)?(?:write|produce|draft|compose|develop|keep|limit|make|ensure|respect|do\s+not\s+exceed|don[\x27’]t\s+exceed)\b', re.I)
_UNSAFE_PREFIX = re.compile(r'\b(?:not|never|avoid|ignore|disregard|example|quoted|previous\s+request|used\s+to|had\s+to)\b', re.I)
_COUNT_SCOPE = re.compile(r'\b(?:exclud\w*|without\s+counting|not\s+counting|do\s+not\s+count)\s+(?:the\s+)?(?:headings?|titles?)\b|\b(?:includ\w*|exclud\w*|count\w*)\b[^.;\n]{0,30}\b(?:references?|bibliograph\w*|citations?|questions?|change\s+notes)\b', re.I)
_STANDARD_EXCLUSION = re.compile(r'\b(?:excluding|without\s+counting|not\s+counting|do\s+not\s+count)\s+(?:the\s+)?(?:separate\s+)?(?:change\s+notes|open\s+questions)(?:\s+and\s+(?:the\s+)?(?:separate\s+)?(?:change\s+notes|open\s+questions))?\b', re.I)
_CANCELLATION = re.compile(r'\b(?:ignore|disregard|remove|cancel|do\s+not\s+(?:enforce|apply))\b[^.;\n]{0,30}\b(?:that|this|the\s+above)\s+(?:limit|target)\b|\b(?:no|without\s+a)\s+word\s+limit\b', re.I)
_SCOPE_QUALIFIER = re.compile(r'\b(?:these|those|the|that|this)\s+(?:bounds?|limits?|targets?|word\s+counts?)\s+(?:apply|applies|refer|refers|relate|relates)\b', re.I)
_HISTORY_HEADING = re.compile(r'^(?:(?:previous|prior|old|earlier|historical)\s+(?:request|instructions?|example|task)|(?:quoted\s+)?example)(?:\s*:\s*|\s*$)', re.I)
_COMPONENT_HEADING = re.compile(r'^(?:(?:for\s+each|per)\s+(?:aim|section|paragraph)|' + _COMPONENT_NAMES + r')(?:\s*:\s*|\s*$)', re.I)
_COMPONENT_TAIL = re.compile(r'^\s*,?\s*(?:only\s+)?(?:per\s+|for\s+(?:the\s+|each\s+)?)(?:aim|section|paragraph|' + _COMPONENT_NAMES + r')\b', re.I)
_CURRENT_HEADING = re.compile(r'^(?:current\s+(?:task|request|instructions?)|for\s+(?:this|the\s+current)\s+task)(?:\s*[:,]\s*|\s*$)', re.I)
_COORDINATE = re.compile(r',?\s+\b(?:and|but)\s+|,\s*', re.I)
_SOFTENING = re.compile(r'(?:not\s+(?:as\s+)?(?:a\s+)?(?:mandatory|hard|strict|required)\s+(?:word\s+)?(?:limit|range|target)|(?:only\s+)?(?:a\s+)?(?:suggested|approximate|indicative)\s+(?:word\s+)?(?:range|limit|target)|as\s+(?:a\s+)?(?:rough|approximate|suggested)\s+(?:target|range|limit))\b', re.I)
_RANGE_REFERENCE = re.compile(r'^(?:this|that|these\s+(?:bounds|limits|targets)|the\s+(?:range|limit|target))\s+(?:is|are|was|were)\b', re.I)


def _trim_clause(text, start):
    shift = 0
    while shift < len(text) and (text[shift].isspace() or text[shift] in '-*•' or
                                unicodedata.category(text[shift]) == 'So'):
        shift += 1
    return start + shift, text[shift:]


def _request_clauses(visible):
    """Yield literal spans with a small explicit heading-scope state machine.

    A historical/component/unknown heading continues until an explicit current-
    task heading. Merely seeing an imperative or main-body label cannot escape.
    Coordinated imperatives are independent directives, never a first-match
    override. Other continuations stay attached for conservative scope checks.
    """
    scope = 'current'
    for clause in re.finditer(r'[^\n;!?]+?(?:(?<!\d)\.(?!\d)|[\n;!?]|$)', visible):
        start, value = _trim_clause(clause[0], clause.start())
        markdown_heading = re.match(r'^#{1,6}[ \t]+', value)
        if markdown_heading:
            start += markdown_heading.end()
            value = value[markdown_heading.end():]
        heading = _CURRENT_HEADING.match(value)
        if heading:
            scope = 'current'
            start, value = _trim_clause(value[heading.end():], start + heading.end())
        else:
            historical = _HISTORY_HEADING.match(value)
            component = _COMPONENT_HEADING.match(value)
            if historical or component:
                heading = historical or component
                scope = 'historical' if historical else 'component'
                start, value = _trim_clause(value[heading.end():], start + heading.end())
            else:
                main_heading = re.match(r'^(?:main\s+(?:draft|body|text)|texte\s+principal|corps\s+du\s+texte)(?:\s*:|\s*$)', value, re.I)
                unknown_heading = markdown_heading or re.fullmatch(r'[^:\n.!?;]{1,120}:\s*', value)
                if unknown_heading and not main_heading and scope == 'current':
                    scope = 'unknown'
        if scope != 'current':
            yield start, value, scope
            continue
        previous = 0
        for connector in _COORDINATE.finditer(value):
            remaining = value[connector.end():]
            if _REQUEST.match(remaining) or _CANCELLATION.match(remaining):
                yield start + previous, value[previous:connector.start()], scope
                previous = connector.end()
        yield start + previous, value[previous:], scope


def _unusual_count_scope(text):
    # These exclusions are already the local body-count convention. They do not
    # authorize excluding headings, references, or other main-draft content.
    return bool(_COUNT_SCOPE.search(_STANDARD_EXCLUSION.sub('', text)))


def _softened_range(text):
    value = text.lstrip(' \t,–—-')
    return bool(_SOFTENING.match(value) or
                (_RANGE_REFERENCE.match(value) and _SOFTENING.search(value)) or
                re.match(r'^(?:roughly|approximately|about|around|environ|approximativement)\b', value, re.I))


def _visible_instruction(text):
    """Mask quoted/code regions without changing Unicode character offsets."""
    characters = list(text)
    fence = None
    offset = 0
    for line in text.splitlines(keepends=True):
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if fence is not None or marker or re.match(r'^\s*>', line):
            for i in range(offset, offset + len(line)):
                if characters[i] not in '\r\n':
                    characters[i] = ' '
            if marker:
                token = marker[1]
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence):
                    fence = None
        offset += len(line)
    value = ''.join(characters)
    i = 0
    while i < len(value):
        char = value[i]
        if char in ('"', '“', '‘', '«', "'", '`'):
            if char == "'" and i and value[i-1].isalnum():
                i += 1
                continue
            close = {'“': '”', '‘': '’', '«': '»'}.get(char, char)
            end = value.find(close, i + 1)
            if end < 0:
                end = len(value) - 1
            for j in range(i, end + 1):
                # Retain sentence boundaries so a following unquoted current
                # directive does not become part of the reported quotation's
                # introductory clause. All quoted letters/numbers stay masked.
                if characters[j] not in '\r\n.;!?':
                    characters[j] = ' '
            i = end + 1
        else:
            i += 1
    return ''.join(characters)


def _quantity(text, forced):
    # The directive grammar accepts whitespace runs, including NBSPs.
    # Normalize only this local interpretation; keep recorded spans literal.
    text = ' '.join(text.split())
    numbers = [int(value) for value in re.findall(r'\d+', text)]
    if len(numbers) == 2:
        return clean({'min_words': numbers[0], 'max_words': numbers[1]})
    if len(numbers) != 1:
        return None
    number = numbers[0]
    if forced == 'maximum' or re.match(r'(?:at most|no more than|up to|au plus)\b', text, re.I):
        return clean({'max_words': number})
    if re.match(r'(?:under|below|less than|fewer than|moins de|sous)\b', text, re.I):
        return clean({'max_words': number - 1})
    if re.match(r'(?:at least|no fewer than|au moins)\b', text, re.I):
        return clean({'min_words': number})
    if re.match(r'(?:exactly|exactement)\b', text, re.I):
        return clean({'min_words': number, 'max_words': number})
    # Bare "600 words" can be a soft target, not an instruction to hit exactly
    # 600 or an independently authorized upper bound.
    return None


def resolve_request(instruction, explicit_target=None):
    """Resolve only explicit CURRENT task directives, never external context.

    This bounded grammar is intentionally incomplete. Advisory prose remains a
    writer instruction; uncertain hard whole-body requests need explicit numeric
    controls. A strictly "under N" request has integer maximum N-1.
    """
    if not isinstance(instruction, str) or len(instruction) > 12000:
        raise ValueError('Instructions must be text of at most 12,000 characters.')
    numeric = clean(explicit_target)
    visible = _visible_instruction(instruction)
    matches, clear, uncertain = [], [], False
    advisory, scope_override, soft_override = False, False, False
    for offset, value, scope in _request_clauses(visible):
        if not value:
            continue
        if scope != 'current':
            advisory = advisory or bool(_WORD_CUE.search(value))
            if scope == 'unknown' and _WORD_CUE.search(value):
                uncertain = True
            continue
        if _RANGE_REFERENCE.match(value) and _SOFTENING.search(value):
            soft_override = True
        if _CANCELLATION.search(value):
            scope_override = True
        if _SCOPE_QUALIFIER.search(value) and _COMPONENT.search(value):
            scope_override = True
        if _unusual_count_scope(value):
            scope_override = True
        found = None
        for pattern, forced, body in _DIRECTIVES:
            found = pattern.match(value)
            if found:
                break
        if found:
            prefix = value[:found.start('amount')]
            allowed_negative = re.match(r'^(?:please\s+)?(?:do\s+not|don[\x27’]t)\s+exceed', value, re.I)
            if _UNSAFE_PREFIX.search(prefix) and not allowed_negative:
                advisory = True
                continue
            try:
                target = _quantity(found['amount'], forced)
            except ValueError:
                uncertain = True
                continue
            tail = value[found.end():]
            explicit_body = bool(re.search(r'\b(?:main|body|specific\s+aims|principal|corps)\b', found[0], re.I))
            component_tail = bool(_COMPONENT_TAIL.match(tail))
            if (not body and _COMPONENT.search(tail)) or (component_tail and not explicit_body):
                advisory = True
                continue
            if (re.match(r'\s+(?:per|for\s+each|for\s+aim\s+\d)\b', tail, re.I)
                    and not re.search(r'\b(?:main|body|specific\s+aims|principal|corps)\b', found[0], re.I)):
                advisory = True
                continue
            if target is None:
                advisory = True
                continue
            start, end = offset + found.start(), offset + found.end()
            matches.append({'start': start, 'end': end, 'text': instruction[start:end], 'target': target})
            if _softened_range(tail):
                advisory = True
                continue
            # A condition immediately qualifying the requested length is
            # uncertain. Scientific content such as "explaining if ..." is not
            # a condition on that length directive.
            ambiguous = bool(re.match(r'\s*[,—-]?\s*(?:only\s+)?(?:if|unless|otherwise|depending|provided\s+that|si|sauf)\b', tail, re.I) or _unusual_count_scope(value) or
                             re.search(r'\bas\s+(?:an?|the)\s+(?:example|quotation)\b', tail, re.I))
            # A main-body request may coexist with a separate summary request;
            # those component numbers are never merged into the body target.
            if component_tail:
                ambiguous = True
            # Additional unparsed hard word wording cannot be silently dropped.
            # An explicitly separate component request is outside this target.
            if _WORD_CUE.search(tail) and not _COMPONENT.search(tail):
                ambiguous = True
            if ambiguous:
                uncertain = True
            else:
                clear.append(target)
            continue
        cue = _WORD_CUE.search(value)
        if cue and (_SOFT.search(value) or _COMPONENT.search(value)):
            advisory = True
        elif cue and _REQUEST.search(value):
            uncertain = True
        elif cue:
            # Reported/quoted historical counts are not current directives.
            advisory = True
        elif re.search(r'\b(?:shorter|longer|shorten|raccourcir)\b|\b(?:one|a|\d+)\s+pages?\b|\bune\s+page\b', value, re.I):
            advisory = True
    if matches and scope_override:
        uncertain = True
    if clear and soft_override:
        if len(clear) == 1:
            clear = []
            advisory = True
        else:
            # The scope of a later softening across several hard directives is
            # unclear. Do not discard one directive or select a surviving bound.
            uncertain = True
    distinct = {tuple(t[key] for key in ('min_words', 'max_words')) for t in clear}
    mismatch = numeric is not None and any(target != numeric for target in clear)
    if len(distinct) > 1 or mismatch:
        status, target = 'conflict', None
        origin = 'numeric_fields_and_instruction' if numeric is not None else 'task_instruction'
        message = 'Explicit task word bounds disagree. Choose one main-draft target; no bounds were intersected or silently preferred.'
    elif numeric is not None:
        status, target = 'resolved', numeric
        origin = 'numeric_fields_and_instruction' if clear and not uncertain else 'numeric_fields'
        message = ('Using the explicit numeric task bounds with the standard main-draft counting convention. '
                   'Uncertain instruction wording was not converted into additional bounds.' if uncertain else
                   'Using the explicit numeric task bounds with the standard main-draft counting convention.')
    elif uncertain:
        status, target, origin = 'unresolved', None, 'task_instruction'
        message = 'A possible hard main-draft word request has unclear bounds or counting scope. Set explicit minimum/maximum controls or clarify the current instruction.'
    elif clear:
        status, target, origin = 'resolved', clear[0], 'task_instruction'
        message = 'Using an explicit current-task main-draft word directive. Ranges are inclusive; strictly under N means at most N-1 words.'
    elif advisory:
        status, target, origin = 'advisory', None, 'task_instruction'
        message = 'Length-related prose was retained as an instruction, but no hard whole-body bounds were established. Soft, relative, component, page and reported counts do not create an automatic word target.'
    else:
        status, target, origin = 'not_requested', None, 'none'
        message = 'No explicit current-task main-draft word target was requested.'
    return {'status': status, 'target': target, 'origin': origin, 'resolver_version': RESOLVER_VERSION,
            'instruction_sha256': hashlib.sha256(instruction.encode('utf-8')).hexdigest(),
            'span_unit': 'unicode_code_points', 'matches': matches, 'message': message}


def clean(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {'min_words', 'max_words'}:
        raise ValueError('A length target must contain only min_words and max_words.')
    result = {key: value.get(key) for key in ('min_words', 'max_words')}
    for number in result.values():
        if number is not None and (type(number) is not int or not 1 <= number <= 40000):
            raise ValueError('Word targets must be whole numbers from 1 to 40,000.')
    lower, upper = result['min_words'], result['max_words']
    if lower is None and upper is None:
        return None
    if lower is not None and upper is not None and lower > upper:
        raise ValueError('The minimum word target cannot exceed the maximum.')
    return result


def assess(candidate, target=None):
    target = clean(target)
    measured = draft_metrics.measure(candidate)
    status, repair, explanation = 'not_requested', False, 'No explicit task word target was requested.'
    if target:
        if measured['scope'] != 'explicit_body':
            status, repair = 'unassessed', True
            explanation = 'The main draft is not structurally separated from commentary; the requested body target cannot be assessed reliably.'
        elif target['min_words'] is not None and measured['words'] < target['min_words']:
            status, repair = 'below_minimum', True
            explanation = f"The main draft has {measured['words']} words, below the requested minimum of {target['min_words']}."
        elif target['max_words'] is not None and measured['words'] > target['max_words']:
            status, repair = 'above_maximum', True
            explanation = f"The main draft has {measured['words']} words, above the requested maximum of {target['max_words']}."
        else:
            status = 'within_target'
            explanation = 'The main draft is within the explicitly requested local word-count bounds.'
    if measured['scope'] == 'explicit_body':
        scope = 'Main draft including its headings; separate change notes and open questions are excluded.'
    elif measured['scope'] == 'legacy_body_before_change_summary':
        scope = 'Legacy text before a recognized change-summary heading; this inferred boundary requires review.'
    else:
        scope = 'Complete recorded Markdown text; a reliable main-draft/commentary boundary is unavailable.'
    return {'status': status, 'target': target, 'measurement': measured,
            'repair_needed': repair, 'explanation': explanation,
            'scope': scope,
            'limitation': 'This local count is not an official funder or Word count and does not establish scientific quality or page-limit compliance.'}


def export_markdown(check):
    measured, target = check.get('measurement') or {}, check.get('target')
    lines = [f"Local draft count: {measured.get('words', 'unavailable')} words.",
             'Assessment: ' + check['status'].replace('_', ' ') + '. ' + check['explanation']]
    if target:
        lower, upper = target.get('min_words'), target.get('max_words')
        lines.append('Requested bounds: minimum ' + (str(lower) if lower is not None else 'not set') +
                     '; maximum ' + (str(upper) if upper is not None else 'not set') + ' words.')
    resolution = check.get('request_resolution')
    origin = resolution.get('origin') if isinstance(resolution, dict) else None
    origins = {
        'numeric_fields': 'explicit numeric task controls',
        'task_instruction': 'the current task instructions, interpreted by the local word-request resolver',
        'numeric_fields_and_instruction': 'matching numeric task controls and current task instructions',
        'none': 'no explicit current-task word target',
    }
    # Only software-owned labels are rendered. Matched instruction text and
    # arbitrary metadata must not become Markdown or an asserted source quote.
    lines.append('Word-request provenance: ' + origins.get(origin, 'not recorded for this older or manually imported version') + '.')
    lines += ['', check['scope'], check['limitation'], '',
              'Counting convention: ' + measured.get('method', draft_metrics.METHOD),
              'Count scope: ' + str(measured.get('scope', 'unavailable'))]
    return '\n\n'.join(lines) + '\n'
