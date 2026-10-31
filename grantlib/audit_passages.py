"""Immutable exact-passage registry prototype for automatic audit requests.

This module does not change production audit prompts or validation. Passage
selection locates supplied text; it does not establish scientific support.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
import re


VERSION = 'audit-passages-v1'
MAX_BLOCK_CHARS = 1200
MAX_QUOTE_CHARS = 2000
MAX_PASSAGES = 4096
MAX_REGISTRY_BYTES = 512 * 1024
ANCHOR_REF_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['passage_id'],
    'properties': {'passage_id': {'type': 'string', 'pattern': '^p_[0-9a-f]{64}$',
                                  'minLength': 66, 'maxLength': 66}},
}


def _json(value, depth=0):
    if depth > 64:
        raise ValueError('Audit passage input is too deeply nested.')
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise ValueError('Audit passage objects require string keys.')
        for item in value.values():
            _json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _json(item, depth + 1)
    elif type(value) not in (str, int, float, bool, type(None)):
        raise ValueError('Audit passage inputs must be JSON values.')
    elif type(value) is float and not math.isfinite(value):
        raise ValueError('Audit passage inputs require finite numbers.')


def _canonical(value):
    _json(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          allow_nan=False, separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('Audit passage inputs require valid JSON and Unicode.') from None


def _hash(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _text_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError('The ' + label + ' must be an object.')
    return value


def _records(values, label):
    if not isinstance(values, list):
        raise ValueError('The ' + label + ' must be a list.')
    identifiers = set()
    for value in values:
        _object(value, label + ' record')
        identifier = value.get('id')
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 300:
            raise ValueError('An audit record requires one bounded, nonempty identifier.')
        if identifier in identifiers:
            raise ValueError('Duplicate audit record identifiers are ambiguous.')
        identifiers.add(identifier)
    return values


def _field_surfaces(payload, allow_directions):
    context = _object(payload.get('context'), 'audit context')
    candidate = _object(payload.get('candidate'), 'audit candidate')
    surfaces = []

    def add(kind, identifier, field, value, *, required=False):
        if not isinstance(value, str) or (required and not value.strip()):
            raise ValueError('An anchorable field requires valid text.')
        if value.strip():
            surfaces.append((kind, identifier, field, value))

    def version(kind, value, identifier):
        _object(value, 'audit version')
        add(kind, identifier, 'markdown', value.get('markdown'), required=True)
        if 'change_summary' in value:
            add(kind, identifier, 'change_summary', value['change_summary'])
        for collection, field in [('claims', 'text'), ('directions', 'description')]:
            if collection == 'directions' and not allow_directions:
                continue
            records = value.get(collection, [])
            if not isinstance(records, list):
                raise ValueError('Version claim and direction fields must be lists.')
            for index, record in enumerate(records):
                _object(record, 'version text record')
                add(kind, identifier, f'{collection}.{index}.{field}', record.get(field), required=True)

    version('candidate', candidate, '')
    parent = context.get('selected_version')
    if parent is not None:
        _records([parent], 'selected version')
        version('parent', parent, parent['id'])
    baseline = payload.get('repair_baseline')
    if baseline is not None:
        _object(baseline, 'repair baseline')
        initial = _object(baseline.get('candidate'), 'initial candidate')
        # Existing baseline hashes intentionally use the established serializer,
        # not this registry's compact canonical-JSON format.
        expected = hashlib.sha256(json.dumps(initial, ensure_ascii=False, sort_keys=True,
                                             allow_nan=False).encode('utf-8')).hexdigest()
        if baseline.get('candidate_sha256') != expected:
            raise ValueError('The repair baseline does not match its exact candidate hash.')
        version('repair_baseline', initial, expected)
    brief = _object(context.get('brief', {}), 'brief')
    for field in sorted(brief):
        value = brief[field]
        if re.fullmatch('[a-z_]+', field) and isinstance(value, str):
            add('brief', '', field, value)
    if 'investigator_instruction' in context:
        add('instruction', '', 'text', context['investigator_instruction'])
    for decision in _records(context.get('investigator_decisions', []), 'investigator decisions'):
        add('decision', decision['id'], 'text', decision.get('text'), required=True)
    packet = _object(context.get('evidence_packet', {}), 'evidence packet')
    for source in _records(packet.get('sources', []), 'source records'):
        if not isinstance(source.get('metadata', {}), dict):
            raise ValueError('Source metadata must be an object.')
        add('source', source['id'], 'text', source.get('text'), required=True)
    return surfaces


def _spans(text):
    """Exact paragraph windows, with bounded splits at existing whitespace.

    Whitespace-only separator lines have no IDs. No punctuation, line endings,
    Unicode normalization or inferred sentence boundaries are introduced.
    """
    paragraphs = []
    offset, start = 0, None
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        if line.strip():
            if start is None:
                start = offset
        elif start is not None:
            paragraphs.append((start, offset)); start = None
        offset = end
    if start is not None:
        paragraphs.append((start, len(text)))
    for start, end in paragraphs:
        while start < end:
            stop = min(start + MAX_BLOCK_CHARS, end)
            if stop < end:
                # Prefer a substantial existing line break, then whitespace.
                lower = start + MAX_BLOCK_CHARS // 2
                newline = text.rfind('\n', lower, stop)
                if newline >= lower:
                    stop = newline + 1
                else:
                    whitespace = next((position for position in range(stop - 1, lower - 1, -1)
                                       if text[position].isspace()), None)
                    if whitespace is not None:
                        stop = whitespace + 1
                if text[stop - 1:stop + 1] == '\r\n':
                    stop -= 1
            if text[start:stop].strip():
                yield start, stop
            start = stop


@dataclass(frozen=True, slots=True, init=False)
class Registry:
    """Built once per request; all exposed mutable data are fresh copies.

    Registry JSON is retained as immutable bytes. The wire schema and caller
    wrapper must separately enforce the model provider's total prompt limit.
    """
    input_sha256: str
    registry_sha256: str
    _payload_bytes: bytes = field(repr=False)
    _registry_bytes: bytes = field(repr=False)

    def __init__(self, *args, **kwargs):
        raise TypeError('Construct an audit registry with Registry.build().')

    @classmethod
    def build(cls, payload, *, allow_directions):
        if type(allow_directions) is not bool:
            raise ValueError('allow_directions must be a boolean.')
        _object(payload, 'audit payload')
        snapshot_bytes = _canonical(payload)
        snapshot = json.loads(snapshot_bytes)
        input_hash = hashlib.sha256(snapshot_bytes).hexdigest()
        records, identifiers = [], set()
        serialized_blocks = 0
        for kind, identifier, field, text in _field_surfaces(snapshot, allow_directions):
            field_hash = _text_hash(text)
            for start, end in _spans(text):
                quote = text[start:end]
                if not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
                    raise ValueError('A registry passage is empty or exceeds the quote bound.')
                locator = {'kind': kind, 'id': identifier, 'field': field, 'start': start, 'end': end,
                           'field_sha256': field_hash, 'quote_sha256': _text_hash(quote)}
                passage_id = 'p_' + _hash({'version': VERSION, 'input_sha256': input_hash,
                                          'allow_directions': allow_directions, **locator})
                if passage_id in identifiers:
                    raise ValueError('Ambiguous passage identifier collision.')
                identifiers.add(passage_id)
                record = {'passage_id': passage_id, **locator, 'text': quote}
                records.append(record)
                serialized_blocks += len(_canonical(record))
                if len(records) > MAX_PASSAGES or serialized_blocks > MAX_REGISTRY_BYTES:
                    raise ValueError('Audit passage registry exceeds its fixed size limit; nothing was truncated.')
        manifest = {'version': VERSION, 'input_sha256': input_hash,
                    'hash_scope': 'canonical_payload_json_utf8_compact',
                    'allow_directions': allow_directions, 'passages': records}
        encoded = _canonical(manifest)
        if len(encoded) > MAX_REGISTRY_BYTES:
            raise ValueError('Audit passage registry exceeds its fixed size limit; nothing was truncated.')
        registry = object.__new__(cls)
        for field, value in [('input_sha256', input_hash),
                             ('registry_sha256', hashlib.sha256(encoded).hexdigest()),
                             ('_payload_bytes', snapshot_bytes), ('_registry_bytes', encoded)]:
            object.__setattr__(registry, field, value)
        return registry

    def check_payload(self, payload):
        if _canonical(payload) != self._payload_bytes:
            raise ValueError('The audit payload differs from this immutable registry snapshot.')

    def to_prompt_data(self):
        manifest = json.loads(self._registry_bytes)
        # Offsets/hashes remain available through binding(); the model chooses
        # only an ID, not offsets or quotes. Do not duplicate local proof fields.
        return {'version': VERSION, 'input_sha256': self.input_sha256,
                'registry_sha256': self.registry_sha256,
                'passages': [{key: row[key] for key in ('passage_id', 'kind', 'id', 'field', 'text')}
                             for row in manifest['passages']]}

    def _record(self, reference):
        if (not isinstance(reference, dict) or set(reference) != {'passage_id'} or
                not isinstance(reference['passage_id'], str) or
                not re.fullmatch('p_[0-9a-f]{64}', reference['passage_id'])):
            raise ValueError('A wire anchor must contain only one exact passage_id.')
        record = next((row for row in json.loads(self._registry_bytes)['passages']
                       if row['passage_id'] == reference['passage_id']), None)
        if record is None:
            raise ValueError('The passage ID does not belong to this registry.')
        return record

    def resolve(self, anchor_ref):
        row = self._record(anchor_ref)
        return {'kind': row['kind'], 'id': row['id'], 'field': row['field'], 'quote': row['text']}

    def binding(self, anchor_ref):
        row = self._record(anchor_ref)
        return {'version': VERSION, 'input_sha256': self.input_sha256,
                'registry_sha256': self.registry_sha256,
                # These are immutable original-input locators, not new source
                # anchors for a later context's citation-alias traversal.
                'input_kind': row['kind'], 'input_id': row['id'], 'input_field': row['field'],
                **{key: value for key, value in row.items() if key not in ('text', 'kind', 'id', 'field')}}
