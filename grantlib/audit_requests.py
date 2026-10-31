"""Experimental passage-selection requests; production defaults stay unchanged.

The provider selects software-owned passages. Local resolution restores the
existing exact-quote reports; it does not verify the reviewer's interpretation.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import json

from . import audit_passages, audit_validation, draft_check, revision_check


MAX_PROMPT_BYTES = 1_500_000
_REFERENCE = revision_check._object({'passage_id': revision_check._text(160)})
_FORMAT = '''
PASSAGE-SELECTION RESPONSE FORMAT:
Return the requested structured report with registry_sha256 equal to the supplied registry hash.
Every before, after, authority and sources anchor is ONLY {passage_id}, selected from the supplied
passage_registry. Do not retype quotations, provide offsets, invent identifiers or repair an ID.
Software will attach the exact original quotation and its actual role and source provenance.
Select the smallest available passage(s) that locate the concern. A passage can contain several
clauses; explain specifically which claim is problematic and why. For a qualification spanning
passages, select both IDs. Read the complete original inputs, not just the passage table.
Passages are lossless text windows, not scientifically validated claims or semantic sentences.
An exact location does not establish relevance, evidentiary support or scientific correctness.

before uses parent passages only, plus repair_baseline passages in new-draft checks when supplied.
after uses candidate passages only. authority uses instruction, decision or brief passages only.
sources uses source passages only. Identical words in different roles are not interchangeable.
An earlier model baseline is not an investigator instruction, verified result or current resource.
Keep the existing finding/commitment fields, categories, severity and resolution choices.
Use empty arrays when appropriate, but never replace a required candidate/parent passage with a
source or authority passage. Do not manufacture findings merely to fill a schema.
'''
_DRAFT_RULES = '''
Return {registry_sha256,summary,findings,limitations}; do not include commitments.
Every finding needs an after passage locating the actual candidate, including for an omission:
select the affected aim or interpretation. before is optional and requires an actual parent or
the separately supplied repair baseline. Do not invent a prior version.
Give specific explanations and actionable repairs or investigator questions. Do not return
state, repair_needed, a truth score or a verified badge; software handles routing.
'''
_REVISION_RULES = '''
Return {registry_sha256,summary,commitments,findings,limitations}.
Each commitment needs a before passage; preserved commitments also need an after passage.
Investigator-decision origins and changed_with_authority dispositions need authority passages.
Each finding needs at least one before or after passage. Other arrays may be empty.
Every omitted, contradicted or unclear commitment requires an explained finding.
Keep explanations concise and specific, with an actionable repair or investigator question.
Do not demand verbatim preservation of nonessential prose. Do not return state, repair_needed,
a truth score or a verified badge; software handles routing.
'''


def _legacy_request(kind, context, candidate, action, baseline):
    if kind == 'draft':
        prompt = draft_check.make_prompt(context, candidate, action=action, repair_baseline=baseline)
    else:
        if baseline is not None:
            raise ValueError('A revision passage request does not accept a repair baseline.')
        prompt = revision_check.make_prompt(context, candidate)
    instructions, raw = prompt.split(draft_check.INPUT_DELIMITER, 1)
    return instructions, json.loads(raw)


def _instructions(kind, legacy):
    # Explicit boundaries keep all existing scientific instructions intact while
    # replacing only the legacy quote-format section. Fail if that contract moves.
    boundary = ('Return {summary,findings,limitations}, without commitments' if kind == 'draft'
                else 'Every anchor is {kind,id,field,quote}')
    if legacy.count(boundary) != 1:
        raise ValueError('The scientific audit instruction format changed; review the passage adapter.')
    scientific = legacy.split(boundary, 1)[0]
    old_marker = draft_check.PROMPT_MARKER if kind == 'draft' else revision_check.PROMPT_MARKER
    marker = ('DRAFT' if kind == 'draft' else 'REVISION') + '_SCIENTIFIC_AUDIT_PASSAGES_V1'
    if not scientific.startswith(old_marker + '\n'):
        raise ValueError('The scientific audit marker changed; review the passage adapter.')
    scientific = marker + scientific[len(old_marker):]
    return scientific + _FORMAT + (_DRAFT_RULES if kind == 'draft' else _REVISION_RULES)


def _wire_schema(kind, registry_hash):
    schema = deepcopy(draft_check.SCHEMA if kind == 'draft' else revision_check.SCHEMA)
    schema['properties']['registry_sha256'] = revision_check._enum(registry_hash)
    schema['required'].append('registry_sha256')
    collections = ('findings',) if kind == 'draft' else ('commitments', 'findings')
    for collection in collections:
        fields = schema['properties'][collection]['items']['properties']
        for name in ('before', 'after', 'authority', 'sources'):
            if name in fields:
                fields[name]['items'] = deepcopy(_REFERENCE)
    return schema


@dataclass(frozen=True, slots=True)
class PassageRequest:
    """One immutable prepared request, validated against its unchanged inputs."""
    kind: str
    action: str | None
    prompt: str = field(repr=False)
    _schema_json: str = field(repr=False)
    _payload_json: str = field(repr=False)
    _registry: audit_passages.Registry = field(repr=False)

    @property
    def schema(self):
        return json.loads(self._schema_json)

    @property
    def payload(self):
        """The original scientific payload, without the appended passage table."""
        return json.loads(self._payload_json)

    def validate_partial(self, output, *, context, candidate, repair_baseline=None):
        _, current = _legacy_request(self.kind, context, candidate, self.action, repair_baseline)
        self._registry.check_payload(current)
        revision_check._shape(output, self.schema)
        module = draft_check if self.kind == 'draft' else revision_check
        module._identifiers(output)
        payload = self.payload
        context, candidate = payload['context'], payload['candidate']
        parent = context.get('selected_version')
        baseline = payload.get('repair_baseline')
        collections = ('findings',) if self.kind == 'draft' else ('commitments', 'findings')

        def validate_item(item, collection):
            bindings = []
            for field in ('before', 'after', 'authority', 'sources'):
                resolved = []
                for reference in item.get(field, []):
                    try:
                        resolved.append(self._registry.resolve(reference))
                        bindings.append((resolved[-1], self._registry.binding(reference)))
                    except ValueError:
                        raise audit_validation.ItemError('invalid_anchor',
                            'An audit passage reference was not available in this exact input.') from None
                if field in item:
                    item[field] = resolved
            if self.kind == 'draft':
                draft_check._validate_item(item, context, candidate, parent, baseline)
            else:
                revision_check._validate_item(item, collection, context, candidate, parent)
            for anchor, binding in bindings:
                anchor['passage_binding'] = binding

        accepted, excluded = audit_validation.partition(output, collections, validate_item)
        if self.kind == 'revision' and (not accepted['findings'] or
                any(item['collection'] == 'findings' for item in excluded)):
            # Same conservative rule as revision_check.validate_partial: the
            # legacy report has no explicit commitment-to-finding relationship.
            retained = []
            for position, item in accepted['commitments']:
                if item['disposition'] in ('omitted', 'contradicted', 'unclear'):
                    excluded.append({'collection': 'commitments', 'position': position,
                                     'error_code': 'unresolved_commitment_link_unavailable'})
                else:
                    retained.append((position, item))
            accepted['commitments'] = retained
        report = deepcopy(output)
        del report['registry_sha256']
        report.update({key: [item for _, item in values] for key, values in accepted.items()})
        report = (draft_check._normalize(report, self.action, baseline) if self.kind == 'draft'
                  else revision_check._normalize(report))
        report = audit_validation.finish(report, output, excluded, module.LIMITATION)
        if excluded and baseline is not None:
            report['limitations'].append(draft_check.BASELINE_LIMITATION)
        registry = self._registry.to_prompt_data()
        report['anchor_registry'] = {key: registry[key] for key in
                                     ('version', 'input_sha256', 'registry_sha256')}
        report['anchor_registry']['passage_count'] = len(registry['passages'])
        report['anchor_registry']['scope'] = (
            'Locally resolved exact text windows from the supplied audit inputs; '
            'not verification of scientific relevance, support or correctness.')
        return report


def _prepare(kind, context, candidate, *, action=None, repair_baseline=None):
    legacy, payload = _legacy_request(kind, context, candidate, action, repair_baseline)
    registry = audit_passages.Registry.build(payload, allow_directions=kind == 'draft')
    table = registry.to_prompt_data()
    prompt = (_instructions(kind, legacy) + draft_check.INPUT_DELIMITER +
              json.dumps({**payload, 'passage_registry': table}, ensure_ascii=False))
    if len(prompt.encode('utf-8')) > MAX_PROMPT_BYTES:
        raise ValueError('The complete passage audit exceeds the model context limit; no text was truncated.')
    schema = _wire_schema(kind, table['registry_sha256'])
    return PassageRequest(kind, action, prompt, json.dumps(schema, ensure_ascii=False),
                          json.dumps(payload, ensure_ascii=False), registry)


def prepare_draft(context, candidate, *, action, repair_baseline=None):
    return _prepare('draft', context, candidate, action=action, repair_baseline=repair_baseline)


def prepare_revision(context, candidate):
    return _prepare('revision', context, candidate)
