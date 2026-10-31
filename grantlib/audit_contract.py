"""Additive experimental defects-only scientific audit contract.

This adapter keeps the existing scientific inputs, exact-anchor validators and
partial-item quarantine. It does not change normal Studio requests, interpret
model prose, or establish whether a reported scientific defect is correct.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from . import audit_requests, draft_check, revision_check


CONTRACT_VERSION = 'scientific_defects_v1'
MAX_PROMPT_BYTES = 1_500_000
INPUT_DELIMITER = draft_check.INPUT_DELIMITER
ANCHOR_FORMATS = frozenset({'quotes', 'passages'})

_CONTRACT = '''
SCIENTIFIC DEFECTS CONTRACT (scientific_defects_v1):
Return contract_version exactly "scientific_defects_v1" in the structured response.
Findings describe actual defects or unresolved scientific inputs requiring investigator input,
not a list of every consequential change. A successful authorized change, a preserved control,
or an appropriately qualified unknown is not a defect merely because it is important.
For revision checks, record successful authorized changes in commitments with disposition
changed_with_authority and exact authority anchors. For new-draft checks, any useful description
of successful authorized changes belongs in the summary, not in findings.
Severity describes the actual defect: major means that defect materially compromises scientific
interpretation, design, attribution or feasibility; minor means a local clarity defect without
changing scientific meaning. Do not assign severity to the importance of a successful change.
Authorization does not excuse a remaining error: a permitted endpoint or resource change with
stale dependent analyses or contradictory interpretations can still require a finding about that
specific defect. A genuine violation of current investigator authority can also be a finding.
Use the appropriate allowed category: design_commitment, interpretation, estimand, source_fidelity
or missing_input. authorized_change is not a finding category in this contract. Do not suppress
a genuine defect merely because it arose while implementing an authorized change.
Keep all existing exact-anchor, attribution, missing-input and repair-scope rules. Do not invent
new facts, experiments, thresholds or investigator decisions. Neither empty findings nor valid
anchors establish scientific correctness or completeness. Return only the requested schema.
'''

_SEVERITY = (
    'Classify the severity of an actual defect, not the importance of a successful change.\n'
    'A major defect materially compromises scientific interpretation, design, attribution or feasibility;\n'
    'a minor defect is a local clarity problem that does not change scientific meaning.')
_OLD_SEVERITY = {
    'draft': ('Classify major findings when scientific interpretation, design, attribution or feasibility is affected;\n'
              'minor findings concern local clarity without changing meaning.'),
    'revision': ('Classify findings as major when they alter scientific interpretation, design, attribution or\n'
                 'feasibility; minor when local clarity can improve without changing the scientific meaning.'),
}


def _canonical(value):
    """Reject non-JSON/non-finite snapshots rather than weakening a binding."""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          separators=(',', ':')).encode('utf-8')
    except (TypeError, ValueError, OverflowError, UnicodeError):
        raise ValueError('An experimental audit requires a JSON-safe, finite input snapshot.') from None


def _digest(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


def _legacy(kind, context, candidate, action, baseline):
    # Validate finite JSON before make_prompt can calculate measurements or
    # serialize an otherwise unsupported value. Neither branch reads sources.
    _canonical({'context': context, 'candidate': candidate, 'repair_baseline': baseline})
    if kind == 'draft':
        prompt = draft_check.make_prompt(context, candidate, action=action, repair_baseline=baseline)
    else:
        if baseline is not None:
            raise ValueError('A revision audit does not accept a separate repair baseline.')
        prompt = revision_check.make_prompt(context, candidate)
    instructions, raw = prompt.split(INPUT_DELIMITER, 1)
    payload = json.loads(raw)
    _canonical(payload)
    return instructions, payload


def _instructions(kind, anchor_format, instructions):
    old_marker = (draft_check.PROMPT_MARKER if kind == 'draft' else revision_check.PROMPT_MARKER)
    if anchor_format == 'passages':
        old_marker = ('DRAFT' if kind == 'draft' else 'REVISION') + '_SCIENTIFIC_AUDIT_PASSAGES_V1'
    if not instructions.startswith(old_marker + '\n') or instructions.count(_OLD_SEVERITY[kind]) != 1:
        raise ValueError('The existing scientific audit instructions changed; review the contract adapter.')
    marker = (('DRAFT' if kind == 'draft' else 'REVISION') +
              '_SCIENTIFIC_DEFECTS_V1_' + anchor_format.upper())
    instructions = marker + instructions[len(old_marker):]
    instructions = instructions.replace(_OLD_SEVERITY[kind], _SEVERITY, 1)
    # Update only the response-field declaration, leaving scientific rules and
    # the complete payload intact. Revision quote instructions have no field list.
    fields = ('registry_sha256,' if anchor_format == 'passages' else '') + 'summary,'
    if kind == 'revision':
        fields += 'commitments,'
    fields += 'findings,limitations'
    declaration = 'Return {' + fields + '}'
    if kind == 'draft' or anchor_format == 'passages':
        if instructions.count(declaration) != 1:
            raise ValueError('The audit response declaration changed; review the contract adapter.')
        instructions = instructions.replace(declaration, 'Return {contract_version,' + fields + '}', 1)
    return instructions + _CONTRACT


def _schema(schema):
    schema = deepcopy(schema)
    categories = schema['properties']['findings']['items']['properties']['category']['enum']
    if categories.count('authorized_change') != 1 or 'contract_version' in schema['properties']:
        raise ValueError('The existing audit schema changed; review the contract adapter.')
    categories.remove('authorized_change')
    schema['properties']['contract_version'] = revision_check._enum(CONTRACT_VERSION)
    schema['required'].append('contract_version')
    return schema


@dataclass(frozen=True, slots=True, init=False)
class PreparedRequest:
    """An immutable experiment request with copy-returning public snapshots."""
    kind: str
    action: str | None
    anchor_format: str
    prompt: str = field(repr=False)
    _schema_json: bytes = field(repr=False)
    _payload_json: bytes = field(repr=False)
    _passage_request: audit_requests.PassageRequest | None = field(repr=False)

    @property
    def schema(self):
        return json.loads(self._schema_json)

    @property
    def payload(self):
        """Exact cleaned scientific input, without format or contract additions."""
        return json.loads(self._payload_json)

    def validate_partial(self, output, *, context, candidate, repair_baseline=None):
        _, current = _legacy(self.kind, context, candidate, self.action, repair_baseline)
        if _canonical(current) != self._payload_json:
            raise ValueError('The experimental audit input changed after request preparation.')
        revision_check._shape(output, self.schema)
        # The global new envelope is checked before adapting it to the existing
        # validator. This does not interpret prose or weaken item quarantine.
        legacy_output = deepcopy(output)
        del legacy_output['contract_version']
        if self._passage_request is not None:
            report = self._passage_request.validate_partial(
                legacy_output, context=context, candidate=candidate, repair_baseline=repair_baseline)
        else:
            # Equality above checks the caller's current inputs. Validate from
            # our frozen snapshot so a later caller mutation cannot change the
            # text or provenance being assessed under this request's binding.
            payload = self.payload
            if self.kind == 'draft':
                baseline = payload.get('repair_baseline')
                report = draft_check.validate_partial(legacy_output, payload['context'], payload['candidate'],
                    action=self.action, repair_baseline=baseline['candidate'] if baseline is not None else None)
            else:
                report = revision_check.validate_partial(legacy_output, payload['context'], payload['candidate'])
        # Hash the untouched NEW wire object, including contract_version. Do not
        # call finish again: that would replace partial baseline limitations.
        report['validation']['raw_report_sha256'] = hashlib.sha256(json.dumps(
            output, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest()
        report['validation']['hash_scope'] = 'canonical_report_json'
        report['contract_version'] = CONTRACT_VERSION
        report['audit_contract'] = {
            'anchor_format': self.anchor_format,
            'input_sha256': hashlib.sha256(self._payload_json).hexdigest(),
            'hash_scope': 'canonical_audit_input_json',
        }
        return report


def _prepare(kind, context, candidate, *, action=None, repair_baseline=None, anchor_format='quotes'):
    if anchor_format not in ANCHOR_FORMATS:
        raise ValueError('Experimental audit anchors must use quotes or passages.')
    instructions, payload = _legacy(kind, context, candidate, action, repair_baseline)
    passage_request = None
    prompt_payload = payload
    if anchor_format == 'passages':
        passage_request = (audit_requests.prepare_draft(context, candidate, action=action,
                           repair_baseline=repair_baseline) if kind == 'draft'
                           else audit_requests.prepare_revision(context, candidate))
        instructions, raw = passage_request.prompt.split(INPUT_DELIMITER, 1)
        prompt_payload = json.loads(raw)
        if _canonical(passage_request.payload) != _canonical(payload):
            raise ValueError('The passage adapter did not preserve the exact scientific input.')
        schema = passage_request.schema
    else:
        schema = draft_check.SCHEMA if kind == 'draft' else revision_check.SCHEMA
    prompt = (_instructions(kind, anchor_format, instructions) + INPUT_DELIMITER +
              json.dumps(prompt_payload, ensure_ascii=False, allow_nan=False))
    if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('The complete experimental audit exceeds the model context limit; no text was truncated.')
    request = object.__new__(PreparedRequest)
    for name, value in {
        'kind': kind, 'action': action, 'anchor_format': anchor_format, 'prompt': prompt,
        '_schema_json': _canonical(_schema(schema)), '_payload_json': _canonical(payload),
        '_passage_request': passage_request,
    }.items():
        object.__setattr__(request, name, value)
    return request


def prepare_draft(context, candidate, *, action, repair_baseline=None, anchor_format='quotes'):
    return _prepare('draft', context, candidate, action=action,
                    repair_baseline=repair_baseline, anchor_format=anchor_format)


def prepare_revision(context, candidate, *, anchor_format='quotes'):
    return _prepare('revision', context, candidate, anchor_format=anchor_format)
