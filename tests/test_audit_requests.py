"""Invented passage selections test scientific-input and provenance boundaries."""
from copy import deepcopy
import hashlib
import json
import unittest
from unittest.mock import patch

from grantlib import audit_requests, draft_check, revision_check
from grantlib.model import _check_schema, _validate
from grantlib.studio import prepare_context
from tests import test_draft_check as dc, test_revision_check as rc


SECRET = 'Excluded invented criticism is unavailable.'


def table(request):
    return json.loads(request.prompt.split(draft_check.INPUT_DELIMITER, 1)[1])['passage_registry']


def wire(request, raw):
    """Select an existing whole passage for an exact test-fixture quotation."""
    result = deepcopy(raw)
    registry = table(request)
    result['registry_sha256'] = registry['registry_sha256']
    for collection in ('findings', 'commitments'):
        for item in result.get(collection, []):
            for group in ('before', 'after', 'authority', 'sources'):
                references = []
                for anchor in item.get(group, []):
                    matches = [p for p in registry['passages']
                               if all(p[key] == anchor[key] for key in ('kind', 'id', 'field'))
                               and anchor['quote'] in p['text']]
                    if not matches:
                        raise AssertionError('The invented fixture has no exact selectable passage.')
                    chosen = min(matches, key=lambda p: len(p['text']))
                    references.append({'passage_id': chosen['passage_id']})
                if group in item:
                    item[group] = references
    return result


class PassageRequestTests(unittest.TestCase):
    def cases(self):
        context, candidate = dc.fixture()
        request = audit_requests.prepare_draft(context, candidate, action='aims')
        yield 'draft', context, candidate, request, dc.report(dc.finding())
        context, candidate = rc.fixture()
        request = audit_requests.prepare_revision(context, candidate)
        raw = rc.clean_report(); raw['findings'] = [rc.omission()]
        yield 'revision', context, candidate, request, raw

    def test_full_scientific_payload_and_instructions_survive_format_change(self):
        for kind, context, candidate, request, raw in self.cases():
            legacy = (draft_check.make_prompt(context, candidate, action='aims') if kind == 'draft'
                      else revision_check.make_prompt(context, candidate))
            old = json.loads(legacy.split(draft_check.INPUT_DELIMITER, 1)[1])
            new = json.loads(request.prompt.split(draft_check.INPUT_DELIMITER, 1)[1])
            new.pop('passage_registry')
            self.assertEqual(new, old)
            self.assertEqual(request.payload, old)
            self.assertIn('SCIENTIFIC_AUDIT_PASSAGES_V1', request.prompt.splitlines()[0])
            self.assertNotIn('Every anchor is {kind,id,field,quote}', request.prompt)
            self.assertIn('Do not manufacture findings', request.prompt)
            self.assertIn('not verification', request.validate_partial(
                wire(request, raw), context=context, candidate=candidate)['anchor_registry']['scope'])

    def test_wire_schema_resolves_to_exact_original_text_and_local_provenance(self):
        for kind, context, candidate, request, raw in self.cases():
            source = context['evidence_packet']['sources'][0]
            raw['findings'][0]['sources'] = [dc.anchor(source['text'], 'source', source['id'], 'text')]
            value = wire(request, raw)
            _check_schema(request.schema)
            _validate(value, request.schema, request.schema)
            result = request.validate_partial(value, context=context, candidate=candidate)
            anchor = result['findings'][0]['sources'][0]
            self.assertEqual(anchor['quote'], source['text'])
            self.assertEqual(anchor['source_hash'], hashlib.sha256(json.dumps(
                source, ensure_ascii=False, sort_keys=True).encode()).hexdigest())
            self.assertEqual(anchor['provenance']['metadata']['scope'], source['metadata']['scope'])
            self.assertTrue(anchor['passage_binding'])
            self.assertEqual(result['validation']['raw_report_sha256'], hashlib.sha256(json.dumps(
                value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest())
            self.assertEqual(result['validation']['status'], 'complete')
            self.assertEqual(result['anchor_registry']['registry_sha256'], value['registry_sha256'])
            exported = (draft_check if kind == 'draft' else revision_check).export_markdown(result)
            self.assertIn(source['text'], exported)
            self.assertIn(source['source_uri'], exported)

    def test_non_english_passage_selection_preserves_words_a_copying_error_lost(self):
        context, candidate = dc.fixture()
        candidate['markdown'] = 'La unidad sería el episodio de salida, con errores que tienen en cuenta los episodios repetidos.'
        request = audit_requests.prepare_draft(context, candidate, action='design')
        item = dc.finding(); item['after'][0]['quote'] = candidate['markdown'].replace(' de salida', '')
        with self.assertRaises(ValueError):
            draft_check.validate(dc.report(item), context, candidate, action='design')
        # The new provider selects the supplied window; software does not guess
        # or repair the old erroneous quotation, which remains unmodified.
        original_bad = deepcopy(item)
        passage = next(p for p in table(request)['passages']
                       if p['kind'] == 'candidate' and p['field'] == 'markdown')
        item['after'] = [{'passage_id': passage['passage_id']}]
        raw = dc.report(item); raw['registry_sha256'] = table(request)['registry_sha256']
        result = request.validate_partial(raw, context=context, candidate=candidate)
        self.assertEqual(result['findings'][0]['after'][0]['quote'], candidate['markdown'])
        self.assertNotIn(' de sortie', original_bad['after'][0]['quote'])

    def test_partial_unknown_reference_discards_whole_item_and_all_rejected_is_unassessed(self):
        for kind, context, candidate, request, raw in self.cases():
            value = wire(request, raw)
            value['findings'][0]['severity'] = 'minor'
            bad = deepcopy(value['findings'][0]); bad.update(id='bad', summary=SECRET,
                explanation=SECRET, repair=SECRET, severity='major')
            group = 'after' if kind == 'draft' else 'before'
            bad[group].append({'passage_id': 'not-a-supplied-passage'})
            value['findings'].append(bad); value['summary'] = SECRET; value['limitations'] = [SECRET]
            snapshot = deepcopy(value)
            result = request.validate_partial(value, context=context, candidate=candidate)
            self.assertEqual(value, snapshot)
            self.assertEqual(len(result['findings']), 1)
            self.assertFalse(result['repair_needed'])
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertNotIn('not-a-supplied-passage', json.dumps(result))
            self.assertEqual(result['validation']['excluded_items'], [
                {'collection': 'findings', 'position': 1, 'error_code': 'invalid_anchor'}])
            value['findings'] = [bad]
            result = request.validate_partial(value, context=context, candidate=candidate)
            self.assertEqual(result['state'], 'unassessed')
            self.assertFalse(result['repair_needed'])

    def test_registry_mismatch_and_fatal_envelope_errors_reject_entire_report(self):
        for kind, context, candidate, request, raw in self.cases():
            value = wire(request, raw)
            for bad in [dict(value, registry_sha256='0' * 64),
                        dict(value, findings=value['findings'] * 2),
                        dict(value, supplied_extra=SECRET)]:
                with self.subTest(kind=kind), self.assertRaises(ValueError):
                    request.validate_partial(bad, context=context, candidate=candidate)

    def test_current_input_or_provenance_change_cannot_reinterpret_old_ids(self):
        for kind, context, candidate, request, raw in self.cases():
            value = wire(request, raw)
            altered = deepcopy(context)
            altered['evidence_packet']['sources'][0]['metadata']['role'] = 'different-role'
            with self.assertRaises(ValueError):
                request.validate_partial(value, context=altered, candidate=candidate)
            altered = deepcopy(candidate); altered['markdown'] += ' A changed final qualification.'
            with self.assertRaises(ValueError):
                request.validate_partial(value, context=context, candidate=altered)

    def test_correctly_located_source_cannot_become_investigator_authority(self):
        for kind, context, candidate, request, raw in self.cases():
            value = wire(request, raw)
            source = next(p for p in table(request)['passages'] if p['kind'] == 'source')
            value['findings'][0]['authority'] = [{'passage_id': source['passage_id']}]
            result = request.validate_partial(value, context=context, candidate=candidate)
            self.assertEqual(result['findings'], [])
            self.assertEqual(result['state'], 'unassessed')

    def test_baseline_is_exact_model_history_and_cannot_become_parent_or_authority(self):
        context, candidate = dc.fixture(); baseline = deepcopy(candidate)
        request = audit_requests.prepare_draft(context, candidate, action='aims', repair_baseline=baseline)
        item = dc.finding(); item['before'] = [dc.anchor(baseline['markdown'], 'repair_baseline',
            draft_check.baseline_snapshot(baseline)['candidate_sha256'])]
        value = wire(request, dc.report(item))
        result = request.validate_partial(value, context=context, candidate=candidate, repair_baseline=baseline)
        self.assertEqual(result['findings'][0]['before'][0]['quote'], baseline['markdown'])
        self.assertIn(draft_check.BASELINE_LIMITATION, result['limitations'])
        self.assertIsNone(request.payload['context']['selected_version'])
        with self.assertRaises(ValueError):
            request.validate_partial(value, context=context, candidate=candidate)
        value['findings'][0]['authority'] = value['findings'][0]['before']
        result = request.validate_partial(value, context=context, candidate=candidate, repair_baseline=baseline)
        self.assertEqual(result['state'], 'unassessed')
        self.assertIn(draft_check.BASELINE_LIMITATION, result['limitations'])

    def test_unresolved_revision_commitment_keeps_conservative_partial_link_rule(self):
        context, candidate = rc.fixture()
        request = audit_requests.prepare_revision(context, candidate)
        raw = rc.clean_report(); raw['findings'] = [rc.omission()]
        first = rc.preserved(); second = deepcopy(first); second.update(id='C2', disposition='unclear')
        raw['commitments'] = [first, second]
        value = wire(request, raw)
        value['findings'][0]['before'] = [{'passage_id': 'unknown'}]
        result = request.validate_partial(value, context=context, candidate=candidate)
        self.assertEqual([item['id'] for item in result['commitments']], ['C1'])
        self.assertEqual(result['state'], 'unassessed')
        self.assertIn('unresolved_commitment_link_unavailable',
                      [item['error_code'] for item in result['validation']['excluded_items']])

    def test_copies_cannot_mutate_prepared_inputs_and_size_failure_does_not_truncate(self):
        context, candidate = dc.fixture()
        request = audit_requests.prepare_draft(context, candidate, action='aims')
        original_prompt = request.prompt
        exposed = request.payload; exposed['candidate']['markdown'] = 'Wrong text'
        exposed_schema = request.schema; exposed_schema['properties'].clear()
        self.assertEqual(request.prompt, original_prompt)
        self.assertEqual(request.payload['candidate'], candidate)
        self.assertIn('findings', request.schema['properties'])
        snapshot = deepcopy((context, candidate))
        with patch.object(audit_requests, 'MAX_PROMPT_BYTES', 1), self.assertRaisesRegex(ValueError, 'no text was truncated'):
            audit_requests.prepare_draft(context, candidate, action='aims')
        self.assertEqual((context, candidate), snapshot)

    def test_exact_but_irrelevant_passage_is_not_scientific_verification(self):
        context, candidate = dc.fixture()
        request = audit_requests.prepare_draft(context, candidate, action='aims')
        raw = dc.report(dc.finding())
        raw['findings'][0]['explanation'] = 'This deliberately irrelevant invented opinion is not scientifically justified.'
        result = request.validate_partial(wire(request, raw), context=context, candidate=candidate)
        self.assertEqual(result['validation']['status'], 'complete')
        self.assertNotIn('scientifically_verified', result)
        self.assertIn('not independent scientific verification', result['limitations'][0])
        self.assertIn('not verification of scientific relevance', result['anchor_registry']['scope'])

    def test_later_source_alias_preserves_original_passage_binding_and_literal_quote(self):
        context, candidate = rc.fixture()
        request = audit_requests.prepare_revision(context, candidate)
        raw = rc.clean_report(); item = rc.omission()
        source = context['evidence_packet']['sources'][0]
        item['sources'] = [dc.anchor(source['text'], 'source', source['id'], 'text')]
        raw['findings'] = [item]
        result = request.validate_partial(wire(request, raw), context=context, candidate=candidate)
        original_anchor = deepcopy(result['findings'][0]['sources'][0])
        artifact = {'id': 'saved-fictional-draft', 'kind': 'aims', 'title': candidate['title'],
                    'markdown': candidate['markdown'], 'structured': candidate,
                    'context': context['evidence_packet'],
                    'model_metadata': {'revision_check': {'status': 'completed', 'report': result}}}
        proposal = {'id': 'invented-proposal', 'artifacts': [artifact], 'decisions': [], 'references': []}
        snapshot = deepcopy(proposal)
        refreshed = deepcopy(source); refreshed['text'] = 'A different later source snapshot.'
        packet, versions = prepare_context(proposal, {'sources': [refreshed]}, 'revise',
                                           {'base_artifact_id': artifact['id']})
        anchor = versions['selected_version']['previous_revision_check']['report']['findings'][0]['sources'][0]
        self.assertNotEqual(anchor['id'], source['id'])
        self.assertEqual(anchor['original_source_id'], source['id'])
        self.assertEqual(anchor['quote'], original_anchor['quote'])
        self.assertEqual(anchor['passage_binding'], original_anchor['passage_binding'])
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == anchor['id'])['text'], source['text'])
        self.assertEqual(proposal, snapshot)


if __name__ == '__main__':
    unittest.main()
