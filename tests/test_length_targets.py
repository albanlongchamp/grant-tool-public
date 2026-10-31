from copy import deepcopy
import unittest

from grantlib.length_targets import clean, assess, export_markdown


class LengthTargetTests(unittest.TestCase):
    def test_explicit_bounds_validate_without_inference_or_coercion(self):
        self.assertIsNone(clean(None))
        self.assertIsNone(clean({}))
        self.assertEqual(clean({'max_words': 350}), {'min_words': None, 'max_words': 350})
        for value in ([], '350', {'max_words': True}, {'min_words': 1.5}, {'max_words': 0},
                      {'min_words': 40001}, {'min_words': 351, 'max_words': 350}, {'pages': 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                clean(value)

    def test_counts_body_only_at_inclusive_bounds(self):
        target = {'min_words': 250, 'max_words': 350}
        for words, status in ((249, 'below_minimum'), (250, 'within_target'),
                              (350, 'within_target'), (351, 'above_maximum')):
            output = {'markdown': 'word ' * words + '[[P_source]]',
                      'change_summary': 'notes ' * 100, 'open_questions': ['question ' * 200]}
            result = assess(output, target)
            self.assertEqual(result['measurement']['words'], words)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['repair_needed'], status != 'within_target')

    def test_source_numbers_and_free_text_cannot_create_a_target(self):
        output = {'markdown': 'A previous source said 350 words; use one page.', 'change_summary': ''}
        result = assess(output)
        self.assertEqual(result['status'], 'not_requested')
        self.assertIsNone(result['target'])
        self.assertFalse(result['repair_needed'])

    def test_legacy_body_boundary_cannot_prove_target_compliance(self):
        output = {'markdown': 'word ' * 300 + '\n\n## Material changes\n\nNotes.'}
        result = assess(output, {'max_words': 350})
        self.assertEqual(result['status'], 'unassessed')
        self.assertTrue(result['repair_needed'])
        self.assertEqual(result['measurement']['words'], 300)
        self.assertIn('inferred boundary requires review', result['scope'])

    def test_unseparated_manual_text_does_not_claim_excluded_commentary(self):
        result = assess({'markdown': 'Main text.\n\nSome commentary without a recognized heading.'})
        self.assertEqual(result['measurement']['scope'], 'full_markdown')
        self.assertIn('Complete recorded Markdown text', result['scope'])
        self.assertNotIn('excluded', result['scope'])

    def test_inputs_immutable_and_export_qualifies_result(self):
        candidate = {'markdown': 'mot ' * 381, 'change_summary': 'Explication.'}
        target = {'min_words': 250, 'max_words': 350}
        before = deepcopy((candidate, target))
        result = assess(candidate, target)
        self.assertEqual((candidate, target), before)
        text = export_markdown(result)
        self.assertIn('381', text)
        self.assertIn('above maximum', text)
        self.assertIn('not an official funder', text)


if __name__ == '__main__':
    unittest.main()
