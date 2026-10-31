"""Invented single-draft findings exercise exact location and source boundaries."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from grantlib import audit_context, draft_check, revision_check
from grantlib.model import _check_schema, _validate


OVERCLAIM = 'These observations will distinguish absent activity from insufficient sensitivity.'


def fixture():
    return ({'brief': {'question': 'Invented fixed-sample study.', 'constraints': 'Live access is unknown.'},
             'investigator_instruction': 'Develop the selected idea without inventing missing access.',
             'investigator_decisions': [{'id': 'decision-1', 'text': 'Use Spanish for this invented proposal.'}],
             'selected_version': None, 'related_versions': [],
             'evidence_packet': {'selected_memory': [], 'sources': [{
                 'id': 'M_fixture', 'title': 'Invented imaging access', 'layer': 'investigator_memory',
                 'text': 'Only fixed-sample imaging is available in this invented facility.',
                 'source_uri': 'urn:invented:memory',
                 'metadata': {'role': 'investigator_statement', 'scope': 'Fixed samples only.',
                              'confirmed_at': '2026-01-01', 'review_after': '',
                              'provenance_note': 'Invented investigator statement, not verified science.',
                              'limitations': ['No live assay access is established.']}}]}},
            {'title': 'Invented aims', 'markdown': OVERCLAIM, 'change_summary': '',
             'claims': [{'text': 'All organoids are independent biological replicates.'}],
             'directions': [{'id': 'D1', 'title': 'Invented direction',
                             'description': 'Live functional testing is immediately feasible.'}],
             'open_questions': []})


def anchor(quote, kind='candidate', identifier='', field='markdown'):
    return {'kind': kind, 'id': identifier, 'field': field, 'quote': quote}


def finding():
    return {'id': 'interpretation-1', 'category': 'interpretation', 'severity': 'major',
            'resolution': 'draft_repair', 'summary': 'Discrimination is overpromised.',
            'explanation': 'The promised distinction requires sensitivity or precision not supplied.',
            'repair': 'Make the interpretation conditional on adequate precision; leave unknown inputs unresolved.',
            'before': [], 'after': [anchor(OVERCLAIM)], 'authority': [], 'sources': []}


def report(*findings):
    return {'summary': 'Limited assessment of an invented draft.',
            'findings': list(findings), 'limitations': []}


class DraftCheckTests(unittest.TestCase):
    def setUp(self):
        self.context, self.candidate = fixture()

    def validate(self, value, action='aims', repair_baseline=None):
        return draft_check.validate(value, self.context, self.candidate, action=action,
                                    repair_baseline=repair_baseline)

    def test_direct_prompt_and_validation_use_same_clean_context_preserving_review_and_baseline(self):
        self.context['selected_version'] = {'id': 'selected-review', 'kind': 'review',
            'markdown': 'The selected review requests a qualified interpretation.',
            'recorded_claim_judgments': [{'explanation': 'An investigator judgment.'}],
            'previous_draft_check': {'report': 'HISTORICAL_AUTOMATIC_OPINION'}}
        self.context['related_versions'] = [{'id': 'older', 'markdown': 'Earlier scientific text.',
            'previous_revision_check': {'report': 'HISTORICAL_AUTOMATIC_OPINION'}}]
        original = deepcopy(self.context)
        baseline = deepcopy(self.candidate)
        expected, _ = audit_context.prepare(original)
        prompt = draft_check.make_prompt(self.context, self.candidate, action='aims', repair_baseline=baseline)
        payload = json.loads(prompt.split(draft_check.INPUT_DELIMITER, 1)[1])
        self.assertEqual(payload['context'], expected)
        self.assertEqual(payload['repair_baseline'], draft_check.baseline_snapshot(baseline))
        self.assertNotIn('HISTORICAL_AUTOMATIC_OPINION', prompt)
        item = finding()
        item['before'] = [anchor(expected['selected_version']['markdown'], 'parent', 'selected-review'),
                          anchor(OVERCLAIM, 'repair_baseline', payload['repair_baseline']['candidate_sha256'])]
        item['sources'] = [anchor(expected['evidence_packet']['sources'][0]['text'], 'source', 'M_fixture', 'text')]
        with patch.object(draft_check, '_inputs', wraps=draft_check._inputs) as inputs:
            validated = self.validate(report(item), repair_baseline=baseline)
        self.assertEqual(inputs.call_args.args[0], expected)
        self.assertEqual(validated['findings'][0]['sources'][0]['quote'], item['sources'][0]['quote'])
        self.assertEqual(self.context, original)
        self.assertEqual(baseline, self.candidate)

    def test_schema_and_clean_report_require_no_parent_or_commitments(self):
        _check_schema(draft_check.SCHEMA)
        _validate(report(), draft_check.SCHEMA, draft_check.SCHEMA)
        result = self.validate(report(), action='design')
        self.assertNotIn('commitments', result)
        self.assertNotIn('parent', result)
        self.assertEqual(result['state'], 'no_issues_reported')
        self.assertFalse(result['repair_needed'])
        self.assertIn('not independent scientific verification', result['limitations'][0])

    def test_exact_conclusion_can_trigger_repair_without_inventing_parent(self):
        value = self.validate(report(finding()))
        self.assertTrue(value['repair_needed'])
        self.assertEqual(value['state'], 'issues_found')
        self.assertEqual(value['findings'][0]['before'], [])

    def test_every_finding_requires_candidate_anchor_even_with_real_parent(self):
        self.context['selected_version'] = {'id': 'real-design', 'markdown': 'An actual selected design.'}
        item = finding()
        item['before'] = [anchor('An actual selected design.', 'parent', 'real-design')]
        item['after'] = []
        with self.assertRaisesRegex(ValueError, 'candidate passage'):
            self.validate(report(item))

    def test_parent_anchor_requires_actual_selected_version_and_exact_id(self):
        item = finding()
        item['before'] = [anchor('An actual selected design.', 'parent', 'real-design')]
        with self.assertRaisesRegex(ValueError, 'without an actual'):
            self.validate(report(item))
        self.context['selected_version'] = {'id': 'other-design', 'markdown': 'An actual selected design.'}
        with self.assertRaisesRegex(ValueError, 'different version'):
            self.validate(report(item))
        self.context['selected_version']['id'] = 'real-design'
        self.assertTrue(self.validate(report(item))['repair_needed'])

    def test_claim_direction_and_change_summary_fields_resolve_exactly(self):
        self.candidate['change_summary'] = 'This proves that all required controls are preserved.'
        for field, quote in [('claims.0.text', self.candidate['claims'][0]['text']),
                             ('directions.0.description', self.candidate['directions'][0]['description']),
                             ('change_summary', self.candidate['change_summary'])]:
            with self.subTest(field=field):
                item = finding()
                item['after'] = [anchor(quote, field=field)]
                self.validate(report(item))
                item['after'][0]['quote'] += ' fabricated'
                with self.assertRaisesRegex(ValueError, 'exactly'):
                    self.validate(report(item))

    def test_out_of_range_or_wrong_field_and_nonempty_candidate_id_are_rejected(self):
        for invalid in [anchor(OVERCLAIM, field='directions.3.description'),
                        anchor(OVERCLAIM, field='claims.1.text'),
                        anchor(OVERCLAIM, field='title'), anchor(OVERCLAIM, identifier='invented-id')]:
            with self.subTest(anchor=invalid):
                item = finding(); item['after'] = [invalid]
                with self.assertRaises(ValueError):
                    self.validate(report(item))

    def test_authority_anchors_bind_exact_record_not_some_other_text(self):
        anchors = [anchor(self.context['investigator_instruction'], 'instruction', field='text'),
                   anchor(self.context['investigator_decisions'][0]['text'], 'decision', 'decision-1', 'text'),
                   anchor('Live access is unknown.', 'brief', field='constraints')]
        for value in anchors:
            with self.subTest(kind=value['kind']):
                item = finding(); item['authority'] = [value]
                self.validate(report(item))
                value['quote'] = 'Not the actual authority statement.'
                with self.assertRaisesRegex(ValueError, 'exactly'):
                    self.validate(report(item))

    def test_source_provenance_is_attached_from_snapshot_not_model(self):
        item = finding()
        source = self.context['evidence_packet']['sources'][0]
        item['sources'] = [anchor(source['text'], 'source', 'M_fixture', 'text')]
        original = deepcopy(item)
        result = self.validate(report(item))
        saved = result['findings'][0]['sources'][0]
        self.assertEqual(saved['provenance']['metadata']['scope'], 'Fixed samples only.')
        self.assertEqual(saved['provenance']['metadata']['confirmed_at'], '2026-01-01')
        self.assertEqual(len(saved['source_hash']), 64)
        self.assertEqual(item, original)
        self.assertEqual(draft_check.source_anchors(result), [saved])
        item['sources'][0]['provenance'] = {'invented': 'verification'}
        with self.assertRaises(ValueError):
            self.validate(report(item))

    def test_missing_or_duplicate_source_identity_is_rejected(self):
        item = finding(); source = self.context['evidence_packet']['sources'][0]
        item['sources'] = [anchor(source['text'], 'source', 'missing', 'text')]
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.validate(report(item))
        item['sources'][0]['id'] = 'M_fixture'
        self.context['evidence_packet']['sources'].append(deepcopy(source))
        with self.assertRaisesRegex(ValueError, 'exactly one'):
            self.validate(report(item))

    def test_anchor_kinds_cannot_be_reclassified_as_authority(self):
        item = finding(); item['authority'] = [anchor(OVERCLAIM)]
        with self.assertRaisesRegex(ValueError, 'wrong kind'):
            self.validate(report(item))

    def test_unknown_input_and_minor_finding_do_not_trigger_automatic_repair(self):
        item = finding(); item.update(category='missing_input', resolution='investigator_input')
        self.assertFalse(self.validate(report(item))['repair_needed'])
        item['resolution'] = 'draft_repair'
        with self.assertRaisesRegex(ValueError, 'missing investigator input'):
            self.validate(report(item))
        item = finding(); item['severity'] = 'minor'
        self.assertFalse(self.validate(report(item))['repair_needed'])

    def test_duplicate_findings_and_extra_fields_are_not_accepted(self):
        with self.assertRaisesRegex(ValueError, 'unique'):
            self.validate(report(finding(), finding()))
        invalid = report(); invalid['commitments'] = []
        with self.assertRaisesRegex(ValueError, 'exactly'):
            self.validate(invalid)

    def test_prompt_has_exact_payload_and_no_fake_revision_baseline(self):
        original = deepcopy((self.context, self.candidate))
        prompt = draft_check.make_prompt(self.context, self.candidate, action='design')
        self.assertTrue(prompt.startswith(draft_check.PROMPT_MARKER))
        payload = json.loads(prompt.split(draft_check.INPUT_DELIMITER)[1])
        self.assertEqual(payload, {'action': 'design', 'context': self.context, 'candidate': self.candidate})
        self.assertIsNone(payload['context']['selected_version'])
        self.assertEqual(original, (self.context, self.candidate))
        self.assertIn('does not require verbatim preservation', prompt)
        self.assertIn('Swapping', prompt)

    def test_export_preserves_quotes_and_marks_attributed_single_draft_scope(self):
        item = finding()
        item['sources'] = [anchor(self.context['evidence_packet']['sources'][0]['text'],
                                  'source', 'M_fixture', 'text')]
        rendered = draft_check.export_markdown(self.validate(report(item)))
        self.assertIn('## Automated draft check', rendered)
        self.assertNotIn('Automated revision check', rendered)
        self.assertIn(OVERCLAIM, rendered)
        self.assertIn('Fixed samples only.', rendered)
        self.assertEqual(rendered.count(draft_check.LIMITATION), 1)

    def test_invalid_action_or_inputs_fail_before_prompt_generation(self):
        for action in ['revise', 'review', 'evidence', None]:
            with self.subTest(action=action), self.assertRaises(ValueError):
                self.validate(report(), action=action)
        self.context['selected_version'] = {}
        with self.assertRaises(ValueError):
            self.validate(report())

    def repair_baseline_and_finding(self):
        baseline = deepcopy(self.candidate)
        control = 'Segmentation stability will be measured on repeated blinded annotations.'
        baseline['markdown'] = control + '\n' + baseline['markdown']
        bundle = draft_check.baseline_snapshot(baseline)
        item = finding()
        item.update(category='design_commitment', summary='The explicit reliability measurement disappeared.',
                    explanation='The initial candidate proposed this measurement; the repaired candidate omits it.',
                    repair='Restore the proposed reliability measurement without inventing its thresholds.',
                    before=[anchor(control, 'repair_baseline', bundle['candidate_sha256'])])
        return baseline, bundle, item

    def test_material_loss_anchors_initial_candidate_without_inventing_parent(self):
        baseline, bundle, item = self.repair_baseline_and_finding()
        result = self.validate(report(item), repair_baseline=baseline)
        self.assertIsNone(self.context['selected_version'])
        self.assertTrue(result['repair_needed'])
        self.assertEqual(result['repair_baseline_sha256'], bundle['candidate_sha256'])
        self.assertEqual(result['findings'][0]['before'][0]['kind'], 'repair_baseline')
        self.assertIn(draft_check.BASELINE_LIMITATION, result['limitations'])
        self.assertEqual(draft_check.source_anchors(result), [])

    def test_initial_audit_without_baseline_rejects_baseline_anchor(self):
        _, _, item = self.repair_baseline_and_finding()
        with self.assertRaisesRegex(ValueError, 'requires the initial candidate'):
            self.validate(report(item))

    def test_baseline_hash_and_quote_bind_exact_snapshot_and_field(self):
        baseline, _, item = self.repair_baseline_and_finding()
        item['before'][0]['id'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'different candidate snapshot'):
            self.validate(report(item), repair_baseline=baseline)
        baseline, _, item = self.repair_baseline_and_finding()
        item['before'][0]['quote'] += ' Fabricated continuation.'
        with self.assertRaisesRegex(ValueError, 'exactly'):
            self.validate(report(item), repair_baseline=baseline)
        baseline, _, item = self.repair_baseline_and_finding()
        item['before'][0]['field'] = 'change_summary'
        with self.assertRaisesRegex(ValueError, 'exactly'):
            self.validate(report(item), repair_baseline=baseline)

    def test_baseline_kind_is_before_only_and_does_not_replace_candidate_anchor(self):
        baseline, _, item = self.repair_baseline_and_finding()
        for field in ['after', 'authority', 'sources']:
            with self.subTest(field=field):
                invalid = deepcopy(item)
                invalid[field] = [invalid['before'].pop()]
                with self.assertRaises(ValueError):
                    self.validate(report(invalid), repair_baseline=baseline)
        item['after'] = []
        with self.assertRaisesRegex(ValueError, 'candidate passage'):
            self.validate(report(item), repair_baseline=baseline)

    def test_baseline_never_substitutes_for_the_actual_selected_design(self):
        baseline, bundle, item = self.repair_baseline_and_finding()
        self.context['selected_version'] = {'id': 'actual-design', 'markdown': 'Selected design text.'}
        item['before'].append(anchor('Selected design text.', 'parent', 'actual-design'))
        self.validate(report(item), repair_baseline=baseline)
        item['before'][-1]['id'] = bundle['candidate_sha256']
        with self.assertRaisesRegex(ValueError, 'different version'):
            self.validate(report(item), repair_baseline=baseline)

    def test_baseline_fields_include_claims_directions_and_separate_summary(self):
        baseline, bundle, item = self.repair_baseline_and_finding()
        baseline['change_summary'] = 'A model-authored explanation before repair.'
        bundle = draft_check.baseline_snapshot(baseline)
        for field, quote in [('claims.0.text', baseline['claims'][0]['text']),
                             ('directions.0.description', baseline['directions'][0]['description']),
                             ('change_summary', baseline['change_summary'])]:
            with self.subTest(field=field):
                item['before'] = [anchor(quote, 'repair_baseline', bundle['candidate_sha256'], field)]
                self.validate(report(item), repair_baseline=baseline)

    def test_baseline_snapshot_is_deeply_copied_and_hashes_exact_full_candidate(self):
        baseline, bundle, _ = self.repair_baseline_and_finding()
        reordered = dict(reversed(list(baseline.items())))
        self.assertEqual(bundle, draft_check.baseline_snapshot(reordered))
        bundle['candidate']['claims'][0]['text'] = 'Changed only in the returned copy.'
        self.assertNotEqual(bundle['candidate'], baseline)
        modified = deepcopy(baseline); modified['markdown'] += ' '
        self.assertNotEqual(draft_check.baseline_snapshot(modified)['candidate_sha256'], bundle['candidate_sha256'])
        for invalid in [{}, {'markdown': ''}, {'markdown': 'Text', 'bad': float('nan')},
                        {'markdown': 'Text', 'bad': object()}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                draft_check.baseline_snapshot(invalid)

    def test_final_prompt_has_separate_baseline_without_changing_original_context(self):
        baseline, bundle, _ = self.repair_baseline_and_finding()
        original = deepcopy((self.context, self.candidate, baseline))
        prompt = draft_check.make_prompt(self.context, self.candidate, action='aims', repair_baseline=baseline)
        payload = json.loads(prompt.split(draft_check.INPUT_DELIMITER)[1])
        self.assertEqual(payload['repair_baseline'], bundle)
        self.assertEqual(payload['context'], self.context)
        self.assertEqual(payload['candidate'], self.candidate)
        self.assertEqual(original, (self.context, self.candidate, baseline))
        initial = json.loads(draft_check.make_prompt(self.context, self.candidate, action='aims').split(draft_check.INPUT_DELIMITER)[1])
        self.assertNotIn('repair_baseline', initial)

    def test_draft_only_schema_extension_and_readable_export_preserve_literal_quotes(self):
        original_kinds = revision_check.FINDING['properties']['before']['items']['properties']['kind']['enum']
        self.assertNotIn('repair_baseline', original_kinds)
        baseline, bundle, item = self.repair_baseline_and_finding()
        literal = 'Before — repair_baseline quoted literally [[P_example]] `marker`.'
        baseline['markdown'] += '\n' + literal
        bundle = draft_check.baseline_snapshot(baseline)
        item['before'] = [anchor(literal, 'repair_baseline', bundle['candidate_sha256'])]
        validated = self.validate(report(item), repair_baseline=baseline)
        original = deepcopy(validated)
        rendered = draft_check.export_markdown(validated)
        self.assertIn('Before — initial candidate before repair ' + bundle['candidate_sha256'], rendered)
        self.assertIn('```text\n' + literal + '\n```', rendered)
        self.assertEqual(validated, original)


if __name__ == '__main__':
    unittest.main()
