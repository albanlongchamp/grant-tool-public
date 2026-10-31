import copy
import json
import unittest
from unittest.mock import patch

from grantlib import audit_context, revision_check
from grantlib.model import _check_schema, _validate


def fixture():
    context = {
        'brief': {'title': 'Invented cell experiment',
                  'question': 'Can a fictional perturbation change cell morphology?',
                  'constraints': 'The intervention concentration remains unknown.'},
        'investigator_decisions': [{'id': 'decision-1', 'text': 'Remove the unavailable sequencing assay.'}],
        'investigator_instruction': 'Shorten the draft while preserving the experimental design.',
        'selected_direction': 'mechanism',
        'selected_version': {
            'id': 'parent-1', 'kind': 'aims',
            'markdown': ('Treatment will be balanced across processing batches. Imaging will be blinded. '
                         'Subjects are the independent experimental units. Sequencing is proposed.'),
            'claims': [{'text': 'The collaborator proposes blinded imaging.'}]},
        'related_versions': [],
        'evidence_packet': {'sources': [{
            'id': 'P_fixture', 'title': 'Invented collaborator plan', 'layer': 'personal',
            'text': 'We propose blinded imaging, not completed findings. Literal marker [[example]].',
            'source_uri': 'urn:invented:collaborator',
            'metadata': {'role': 'collaborator_or_template', 'evidence_status': 'proposed',
                         'locator': 'Invented paragraph 1', 'scope': 'retained_excerpt', 'truncated': True}}]},
    }
    candidate = {
        'title': 'Shorter invented aims',
        'markdown': ('Imaging will be blinded. Subjects are the independent experimental units. '
                     'An imprecise contrast will establish that the perturbation has no effect.'),
        'claims': [{'text': 'The collaborator proposes blinded imaging.'}],
        'directions': [], 'open_questions': ['Which intervention concentration is feasible?']}
    return context, candidate


def anchor(kind, quote, *, field=None, identifier=None):
    return {'kind': kind,
            'id': identifier if identifier is not None else {'parent': 'parent-1', 'source': 'P_fixture',
                                                            'decision': 'decision-1'}.get(kind, ''),
            'field': field or ('markdown' if kind in ('parent', 'candidate') else 'text'),
            'quote': quote}


def clean_report():
    return {'summary': 'No additional issue identified in this limited comparison.',
            'commitments': [], 'findings': [], 'limitations': []}


def preserved():
    quote = 'Imaging will be blinded.'
    return {'id': 'C1', 'description': 'Retain imaging blinding.', 'origin': 'prior_proposal',
            'disposition': 'preserved', 'before': [anchor('parent', quote)],
            'after': [anchor('candidate', quote)], 'authority': [],
            'explanation': 'The proposed blinding commitment remains explicit.'}


def omission():
    return {'id': 'F1', 'category': 'design_commitment', 'severity': 'major',
            'resolution': 'draft_repair', 'summary': 'Batch balancing disappeared.',
            'explanation': 'The parent proposed allocation across batches; the candidate omits it.',
            'repair': 'Restore the proposed batch-allocation rule.',
            'before': [anchor('parent', 'Treatment will be balanced across processing batches.')],
            'after': [], 'authority': [], 'sources': []}


class RevisionCheckTests(unittest.TestCase):
    def setUp(self):
        self.context, self.candidate = fixture()

    def validate(self, report):
        return revision_check.validate(report, self.context, self.candidate)

    def test_direct_prompt_and_validation_exclude_only_historical_checks(self):
        self.context['selected_version'].update(kind='review', previous_draft_check={'report':'OLD_AUTOMATIC_OPINION'},
            recorded_claim_judgments=[{'explanation':'Keep this investigator judgment.'}])
        self.context['related_versions'] = [{'id':'explicit-review','kind':'review',
            'markdown':'Retain this deliberately supplied review.', 'previous_revision_check':{'report':'OLD_AUTOMATIC_OPINION'}}]
        original = copy.deepcopy(self.context)
        expected, _ = audit_context.prepare(original)
        prompt = revision_check.make_prompt(self.context, self.candidate)
        payload = json.loads(prompt.split(revision_check.INPUT_DELIMITER, 1)[1])
        self.assertEqual(payload['context'], expected)
        self.assertNotIn('OLD_AUTOMATIC_OPINION', prompt)
        raw = clean_report()
        item = omission()
        item['sources'] = [anchor('source', self.context['evidence_packet']['sources'][0]['text'])]
        raw['findings'] = [item]
        with patch.object(revision_check, '_inputs', wraps=revision_check._inputs) as inputs:
            validated = self.validate(raw)
        self.assertEqual(inputs.call_args.args[0], expected)
        self.assertEqual(validated['findings'][0]['before'], item['before'])
        self.assertEqual(self.context, original)

    def test_clean_is_limited_no_issues_reported_and_not_verification(self):
        report = self.validate(clean_report())
        self.assertEqual(report['state'], 'no_issues_reported')
        self.assertFalse(report['repair_needed'])
        self.assertIn('not independent scientific verification', report['limitations'][0])
        self.assertNotIn('verified', report)

    def test_major_omission_routes_to_repair_with_exact_parent_anchor(self):
        raw = clean_report()
        raw['findings'] = [omission()]
        original = copy.deepcopy(raw)
        report = self.validate(raw)
        self.assertEqual(report['state'], 'issues_found')
        self.assertTrue(report['repair_needed'])
        self.assertEqual(raw, original)
        self.assertEqual(report['findings'][0]['after'], [])

    def test_missing_input_is_not_permission_to_invent_a_repair(self):
        raw = clean_report()
        finding = omission()
        finding.update(category='missing_input', resolution='investigator_input',
                       summary='The concentration remains unspecified.',
                       repair='Ask which concentration is feasible.',
                       authority=[anchor('brief', 'The intervention concentration remains unknown.',
                                         field='constraints')])
        raw['findings'] = [finding]
        report = self.validate(raw)
        self.assertEqual(report['state'], 'issues_found')
        self.assertFalse(report['repair_needed'])
        finding['resolution'] = 'draft_repair'
        with self.assertRaisesRegex(ValueError, 'missing investigator input'):
            self.validate(raw)

    def test_minor_finding_is_visible_without_automatic_repair(self):
        raw = clean_report()
        raw['findings'] = [{**omission(), 'severity': 'minor'}]
        report = self.validate(raw)
        self.assertEqual(report['state'], 'issues_found')
        self.assertFalse(report['repair_needed'])

    def test_real_semantic_error_can_be_anchored_in_conclusion_not_only_claim_list(self):
        raw = clean_report()
        finding = omission()
        finding.update(category='interpretation', before=[],
                       after=[anchor('candidate', 'An imprecise contrast will establish that the perturbation has no effect.')],
                       summary='Imprecision is interpreted as no effect.',
                       explanation='An uninformative contrast cannot discriminate the proposed alternatives.',
                       repair='State that this interpretation remains unresolved.')
        raw['findings'] = [finding]
        self.assertTrue(self.validate(raw)['repair_needed'])

    def test_altered_or_fabricated_quotes_are_rejected_in_every_anchor_kind(self):
        originals = [
            anchor('parent', 'Imaging will be blinded.'),
            anchor('candidate', 'Imaging will be blinded.'),
            anchor('instruction', 'Shorten the draft while preserving the experimental design.'),
            anchor('decision', 'Remove the unavailable sequencing assay.'),
            anchor('brief', 'The intervention concentration remains unknown.', field='constraints'),
            anchor('source', 'We propose blinded imaging, not completed findings.'),
        ]
        for item in originals:
            with self.subTest(kind=item['kind']):
                raw = clean_report()
                finding = omission()
                field = {'parent': 'before', 'candidate': 'after', 'source': 'sources'}.get(item['kind'], 'authority')
                finding[field] = [copy.deepcopy(item)]
                raw['findings'] = [finding]
                self.validate(raw)
                finding[field][0]['quote'] = item['quote'].replace('.', '...')
                with self.assertRaisesRegex(ValueError, 'does not occur exactly'):
                    self.validate(raw)

    def test_quote_present_elsewhere_does_not_validate_wrong_version_or_field(self):
        raw = clean_report()
        raw['commitments'] = [preserved()]
        raw['commitments'][0]['before'][0]['id'] = 'other-version'
        with self.assertRaisesRegex(ValueError, 'different version'):
            self.validate(raw)
        raw['commitments'][0]['before'][0] = anchor('parent', 'Imaging will be blinded.', field='claims.0.text')
        with self.assertRaisesRegex(ValueError, 'does not occur exactly'):
            self.validate(raw)

    def test_structured_claim_and_source_counting_units_are_exactly_locatable(self):
        raw = clean_report()
        finding = omission()
        quote = 'The collaborator proposes blinded imaging.'
        finding.update(category='source_fidelity',
                       before=[anchor('parent', quote, field='claims.0.text')],
                       after=[anchor('candidate', quote, field='claims.0.text')],
                       sources=[anchor('source', 'We propose blinded imaging, not completed findings.')])
        raw['findings'] = [finding]
        normalized = self.validate(raw)
        retained = normalized['findings'][0]['sources'][0]
        self.assertEqual(retained['provenance']['metadata']['role'], 'collaborator_or_template')
        self.assertEqual(retained['provenance']['metadata']['evidence_status'], 'proposed')
        self.assertTrue(retained['provenance']['metadata']['truncated'])
        self.assertEqual(len(retained['source_hash']), 64)

    def test_archival_source_cannot_become_investigator_authority(self):
        raw = clean_report()
        commitment = preserved()
        commitment.update(disposition='changed_with_authority',
                          authority=[anchor('source', 'We propose blinded imaging, not completed findings.')])
        raw['commitments'] = [commitment]
        with self.assertRaisesRegex(ValueError, 'authority contains an anchor of the wrong kind'):
            self.validate(raw)

    def test_explicit_authorized_removal_does_not_require_a_candidate_quote(self):
        raw = clean_report()
        commitment = preserved()
        commitment.update(description='Remove the unavailable assay.', disposition='changed_with_authority',
                          before=[anchor('parent', 'Sequencing is proposed.')], after=[],
                          authority=[anchor('decision', 'Remove the unavailable sequencing assay.')])
        raw['commitments'] = [commitment]
        report = self.validate(raw)
        self.assertEqual(report['state'], 'no_issues_reported')
        self.assertFalse(report['repair_needed'])

    def test_preservation_and_unexplained_changes_cannot_be_asserted_without_anchors_or_findings(self):
        raw = clean_report()
        raw['commitments'] = [preserved()]
        raw['commitments'][0]['after'] = []
        with self.assertRaisesRegex(ValueError, 'candidate anchor'):
            self.validate(raw)
        raw['commitments'][0]['disposition'] = 'omitted'
        with self.assertRaisesRegex(ValueError, 'explained finding'):
            self.validate(raw)
        raw['findings'] = [omission()]
        self.assertEqual(self.validate(raw)['state'], 'issues_found')

    def test_ambiguous_or_missing_source_ids_reject_snapshot_binding(self):
        raw = clean_report()
        raw['findings'] = [{**omission(), 'sources': [anchor('source', 'We propose blinded imaging')]}]
        self.context['evidence_packet']['sources'].append(copy.deepcopy(self.context['evidence_packet']['sources'][0]))
        with self.assertRaisesRegex(ValueError, 'exactly one supplied record'):
            self.validate(raw)
        self.context['evidence_packet']['sources'] = []
        with self.assertRaisesRegex(ValueError, 'exactly one supplied record'):
            self.validate(raw)

    def test_model_cannot_supply_routing_or_trusted_provenance(self):
        raw = clean_report()
        raw['repair_needed'] = False
        with self.assertRaisesRegex(ValueError, 'declared fields'):
            self.validate(raw)
        raw = clean_report()
        raw['findings'] = [{**omission(), 'sources': [anchor('source', 'We propose blinded imaging')]}]
        raw['findings'][0]['sources'][0]['provenance'] = {'role': 'own_grant'}
        with self.assertRaisesRegex(ValueError, 'declared fields'):
            self.validate(raw)

    def test_schema_works_with_generator_validator_and_prompt_preserves_complete_inputs(self):
        _check_schema(revision_check.SCHEMA)
        raw = clean_report()
        raw['commitments'] = [preserved()]
        raw['findings'] = [omission()]
        _validate(raw, revision_check.SCHEMA)
        prompt = revision_check.make_prompt(self.context, self.candidate)
        self.assertTrue(prompt.startswith(revision_check.PROMPT_MARKER))
        payload = json.loads(prompt.split(revision_check.INPUT_DELIMITER, 1)[1])
        self.assertEqual(payload['context'], self.context)
        self.assertEqual(payload['candidate'], self.candidate)
        self.assertEqual(payload['length_comparison']['parent_artifact_id'], self.context['selected_version']['id'])
        self.assertEqual(set(payload), {'context', 'candidate', 'length_comparison'})

    def test_export_quotes_are_literal_and_source_limitations_survive(self):
        literal = 'Literal marker [[example]].\n```\nA deliberately embedded fence.'
        self.context['evidence_packet']['sources'][0]['text'] += '\n' + literal
        raw = clean_report()
        raw['findings'] = [{**omission(), 'sources': [anchor('source', literal)]}]
        report = self.validate(raw)
        text = revision_check.export_markdown(report)
        self.assertTrue(text.startswith('## Automated revision check'))
        self.assertIn(literal, text)
        self.assertIn('````text\n' + literal + '\n````', text)
        for value in ['collaborator_or_template', 'proposed', 'retained_excerpt', 'truncated=true',
                      'urn:invented:collaborator', 'not independent scientific verification']:
            self.assertIn(value, text)
        anchors = revision_check.source_anchors(report)
        self.assertEqual(len(anchors), 1)
        anchors[0]['quote'] = 'Modified output copy'
        self.assertEqual(report['findings'][0]['sources'][0]['quote'], literal)

    def test_identical_wording_in_a_new_version_does_not_override_declared_parent_identity(self):
        raw = clean_report()
        raw['commitments'] = [preserved()]
        self.context['selected_version']['id'] = 'parent-2'
        with self.assertRaisesRegex(ValueError, 'different version'):
            self.validate(raw)

    def test_multiple_findings_export_after_source_provenance_and_notices(self):
        self.context['evidence_packet']['sources'][0]['metadata'].update(
            notices={'coverage': 'Fictional notice coverage only.'},
            limitations=['Only the supplied invented excerpt was assessed.'])
        raw = clean_report()
        raw['commitments'] = [preserved()]
        first = omission()
        first['sources'] = [anchor('source', 'We propose blinded imaging, not completed findings.')]
        second = {**omission(), 'id': 'F2', 'category': 'interpretation',
                  'summary': 'Do not infer no effect from imprecision.', 'before': [],
                  'after': [anchor('candidate', 'An imprecise contrast will establish that the perturbation has no effect.')],
                  'sources': [anchor('source', 'We propose blinded imaging, not completed findings.')]}
        raw['findings'] = [first, second]
        text = revision_check.export_markdown(self.validate(raw))
        self.assertIn('C1: Retain imaging blinding.', text)
        self.assertIn('F1: Batch balancing disappeared.', text)
        self.assertIn('F2: Do not infer no effect from imprecision.', text)
        self.assertEqual(text.count('Assessment: major; draft_repair.'), 2)
        self.assertEqual(text.count('Source notices:'), 2)
        self.assertEqual(text.count('Source limitations:'), 2)
        self.assertEqual(text.count('collaborator_or_template / proposed'), 2)


if __name__ == '__main__':
    unittest.main()
