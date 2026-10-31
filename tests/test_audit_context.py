"""Automatic reviews exclude designated opinions, never scientific evidence."""
from copy import deepcopy
import json
import unittest

from grantlib.audit_context import prepare


class AuditContextTests(unittest.TestCase):
    def test_only_designated_direct_fields_are_removed_without_mutation(self):
        context = {
            'selected_version': {'id': 'selected-review', 'kind': 'review',
                'markdown': 'The deliberately selected review requests blinded scoring.',
                'claims': [{'text': 'Keep subject pairing.'}],
                'recorded_claim_judgments': [{'actor': 'investigator', 'explanation': 'Retain this judgment.'}],
                'previous_draft_check': {'report': 'Old automatic opinion.'},
                'previous_revision_check': None,
                'nested': {'previous_draft_check': 'An unrelated nested field is preserved.'}},
            'related_versions': [{'id': 'old-aims', 'markdown': 'Paired samples.',
                'previous_revision_check': {'report': 'Another automatic opinion.'}},
                {'id': 'explicit-review', 'kind': 'review', 'markdown': 'An explicitly supplied review.'}],
            'brief': {'constraints': 'Unknown assay access.'},
            'investigator_decisions': [{'text': 'Keep six subjects.'}],
            'investigator_instruction': 'Develop the selected review.',
            'evidence_packet': {'sources': [{'text': 'An exact source excerpt.',
                'metadata': {'previous_draft_check': 'Preserve source metadata literally.'}}],
                'selected_memory': [{'text': 'A scoped investigator statement.'}]},
            'repair_baseline': {'candidate': {'markdown': 'The exact original candidate.',
                'previous_draft_check': 'Do not recursively clean the baseline.'}},
            'previous_draft_check': 'A similarly named top-level field is outside this operation.',
        }
        original = deepcopy(context)
        cleaned, paths = prepare(context)
        expected = deepcopy(context)
        del expected['selected_version']['previous_draft_check']
        del expected['selected_version']['previous_revision_check']
        del expected['related_versions'][0]['previous_revision_check']
        self.assertEqual(cleaned, expected)
        self.assertEqual(context, original)
        self.assertEqual(paths, ['/selected_version/previous_draft_check',
                                '/selected_version/previous_revision_check',
                                '/related_versions/0/previous_revision_check'])
        self.assertEqual(prepare(cleaned), (cleaned, []))
        cleaned['evidence_packet']['sources'][0]['text'] = 'Modified copy only.'
        cleaned['selected_version']['claims'][0]['text'] = 'Modified copy only.'
        self.assertEqual(context, original)

    def test_missing_or_null_versions_are_not_invented(self):
        for context in ({}, {'selected_version': None, 'related_versions': []},
                        {'related_versions': [None, 'preserve unrelated values']}):
            with self.subTest(context=context):
                self.assertEqual(prepare(context), (context, []))
        with self.assertRaises(ValueError):
            prepare([])

    def test_shared_version_and_list_aliases_do_not_change_other_evidence_or_selected_review(self):
        version = {'id':'shared','markdown':'Literal scientific text.',
                   'previous_draft_check':{'report':'Literal content outside targeted fields.'}}
        versions = [version]
        context = {'selected_version':version,'selected_review':version,
                   'related_versions':versions,'evidence_packet':{'sources':versions}}
        original_json = json.dumps(context, sort_keys=True)
        cleaned, paths = prepare(context)
        self.assertNotIn('previous_draft_check', cleaned['selected_version'])
        self.assertNotIn('previous_draft_check', cleaned['related_versions'][0])
        self.assertEqual(cleaned['selected_review'], version)
        self.assertEqual(cleaned['evidence_packet']['sources'], versions)
        self.assertIsNot(cleaned['selected_version'], cleaned['selected_review'])
        self.assertIsNot(cleaned['related_versions'], cleaned['evidence_packet']['sources'])
        self.assertEqual(paths, ['/selected_version/previous_draft_check',
                                '/related_versions/0/previous_draft_check'])
        self.assertEqual(json.dumps(context, sort_keys=True), original_json)
