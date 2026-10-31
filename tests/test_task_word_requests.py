"""Synthetic current-task requests; no archive, inference or prose guessing."""
from copy import deepcopy
import hashlib
import unittest

from grantlib.length_targets import RESOLVER_VERSION, assess, export_markdown, resolve_request


class TaskWordRequestTests(unittest.TestCase):
    def assert_target(self, text, minimum, maximum, **options):
        result = resolve_request(text, **options)
        self.assertEqual(result['status'], 'resolved', result)
        self.assertEqual(result['target'], {'min_words': minimum, 'max_words': maximum})
        self.assertEqual(result['instruction_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result['resolver_version'], RESOLVER_VERSION)
        self.assertEqual(result['span_unit'], 'unicode_code_points')
        for match in result['matches']:
            self.assertEqual(text[match['start']:match['end']], match['text'])
        return result

    def test_trial_natural_positive_forms_are_explicit_body_ranges(self):
        for text in (
            'Write a 450–650-word main draft.',
            'Write a 450–650-word Specific Aims main draft.',
            'Produce a 450-650-word main draft with separate change notes and unresolved inputs, preserving participant linkage.',
            'Develop the first listed direction into a coherent Specific Aims main draft of 450–650 words.',
            'Develop the selected direction into a coherent Specific Aims main draft of 450–650 words. Keep change notes and open questions outside the body.',
            'Write the main draft in between 450 and 650 words.',
            'Main draft: 450–650 words.',
        ):
            with self.subTest(text=text):
                self.assert_target(text, 450, 650)

    def test_strict_and_inclusive_bounds_and_exact_request(self):
        for text, low, high in (
            ('Keep the draft under 650 words.', None, 649),
            ('Keep the main draft at most 650 words.', None, 650),
            ('Keep the draft at  most 650 words.', None, 650),
            ('Write no\tmore than 650 words.', None, 650),
            ('Keep the main text to at\u00a0most 650 words.', None, 650),
            ('Do not exceed 650 words.', None, 650),
            ("Don't exceed 650 words.", None, 650),
            ('Limit the draft to 650 words.', None, 650),
            ('Write at least 450 words.', 450, None),
            ('Write exactly 600 words.', 600, 600),
        ):
            with self.subTest(text=text):
                self.assert_target(text, low, high)

    def test_apostrophes_in_directives(self):
        self.assert_target('Please don’t exceed 650 words.', None, 650)
        self.assert_target("Please don't exceed 650 words.", None, 650)

    def test_quotes_fences_and_blockquotes_never_create_targets(self):
        for text in (
            '"Write a 450–650-word main draft."',
            '“Write a 450–650-word main draft.”',
            "'Write a 450–650-word main draft.'",
            '`Write a 450–650-word main draft.`',
            '```text\nWrite a 450–650-word main draft.\n```',
            '~~~\nDo not exceed 650 words.\n~~~',
            '> Write a 450–650-word main draft.',
            'The previous draft contained 715 words.',
            'Do not use the previous 450–650-word limit.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertIsNone(result['target'])
                self.assertIn(result['status'], ('not_requested', 'advisory'))
                self.assertEqual(result['matches'], [])

    def test_positive_directive_ignores_quoted_or_rejected_old_range(self):
        for text in (
            'Earlier draft 715 words. Write a 450–650-word main draft.',
            'The old note said "Write a 200–300-word main draft." Write 450–650 words.',
            'Do not use the previous 200–300-word limit. Write 450–650 words.',
            '> Write 200–300 words.\nWrite 450–650 words.',
        ):
            with self.subTest(text=text):
                self.assertEqual(len(self.assert_target(text, 450, 650)['matches']), 1)

    def test_soft_relative_component_page_and_bare_count_are_advisory(self):
        for text in ('Aim for about 600 words.', 'Write roughly 600 words.', 'Each aim 150–200 words.',
                     'Write 150–200 words per aim.', 'Write a 150–200-word draft for each aim.',
                     'Make it 20% shorter.', 'Shorten the draft.', 'Use one page.', 'Write 600 words.'):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'advisory', result)
                self.assertIsNone(result['target'])

    def test_separate_component_does_not_change_explicit_body_range(self):
        result = self.assert_target('Write a 450–650-word main draft and a 100-word summary.', 450, 650)
        self.assertEqual(len(result['matches']), 1)

    def test_coordinated_cancellation_and_bounds_never_silently_choose_first(self):
        text = 'Write a 450–650-word main draft, but ignore that limit.'
        self.assertEqual(resolve_request(text)['status'], 'unresolved')
        for text in (
            'Write a 450–650-word main draft, and do not exceed 400 words.',
            'Write a 450–650-word main draft, do not exceed 400 words.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'conflict', result)
                self.assertIsNone(result['target'])
                self.assertEqual(len(result['matches']), 2)
                for match in result['matches']:
                    self.assertEqual(text[match['start']:match['end']], match['text'])
        # Elliptical complementary bounds need explicit controls rather than
        # silently applying just the first half of the request.
        self.assertEqual(resolve_request('Write at least 450 words and at most 650 words.')['status'], 'unresolved')

    def test_explicit_multiline_history_or_component_scope_is_retained(self):
        for text in (
            'For each aim:\nWrite between 450 and 650 words.',
            'Previous request:\nWrite a 450–650-word main draft.\nFor this task, focus on clarity without a word limit.',
            'Previous request: «\nWrite a 450–650-word main draft.\n»\nFocus on clarity.',
            'Example:\nWrite a 450–650-word main draft.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertIsNone(result['target'], result)
                self.assertIn(result['status'], ('advisory', 'not_requested'))
                self.assertEqual(result['matches'], [])
        text = 'Previous request:\nWrite 200–300 words.\nCurrent task: Write 450–650 words.'
        result = self.assert_target(text, 450, 650)
        self.assertEqual(len(result['matches']), 1)

    def test_component_and_later_scope_qualifiers_do_not_bind_whole_body(self):
        result = resolve_request('Write a text of at most 650 words for the background section.')
        self.assertEqual(result['status'], 'advisory')
        self.assertIsNone(result['target'])
        for text in (
            'Write a 450–650-word main draft for each aim.',
            'Write between 450 and 650 words. These bounds apply only to Aim 1.',
            'Write between 450 and 650 words. Do not count headings.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'unresolved', result)
                self.assertIsNone(result['target'])

    def test_scientific_conditions_and_standard_note_exclusions_are_not_length_conditions(self):
        for text in (
            'Write a 450–650-word main draft explaining if the intervention changes activity.',
            'Write a 450–650-word main draft excluding separate change notes and open questions.',
            'Write a 450–650-word main draft. Do not count separate change notes and open questions.',
        ):
            with self.subTest(text=text):
                self.assert_target(text, 450, 650)

    def test_trailing_approximation_and_possessive_apostrophe(self):
        self.assertEqual(resolve_request('Write a 450–650-word main draft, roughly.')['status'], 'advisory')
        self.assert_target("Explain the subjects' findings. Write a 450–650-word main draft.", 450, 650)
        self.assertIsNone(resolve_request('«Write a 450–650-word main draft.»')['target'])

    def test_main_body_label_cannot_escape_historical_or_component_heading(self):
        for text in (
            'Previous request:\nMain draft: 450–650 words.\nFor this task, focus on clarity.',
            '## Previous request\nWrite a 450–650-word main draft.',
            'Methods:\nWrite at most 300 words.',
            '## Methods\nWrite at most 300 words.',
            'Methods:\nMain draft: 450–650 words.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertIsNone(result['target'], result)
                self.assertIn(result['status'], ('advisory', 'not_requested'))
                self.assertEqual(result['matches'], [])
        self.assert_target('Previous request:\nMain draft: 200–300 words.\nCurrent task: Write 450–650 words.', 450, 650)

    def test_unknown_heading_scope_requires_explicit_current_task_boundary(self):
        for text in (
            'Some other section:\nWrite at most 300 words.',
            '## An arbitrary heading\nWrite a 450–650-word main draft.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'unresolved', result)
                self.assertIsNone(result['target'])
                numeric = self.assert_target(text, None, 700, explicit_target={'max_words': 700})
                self.assertEqual(numeric['origin'], 'numeric_fields')
        self.assert_target('## An arbitrary heading\nPrior notes.\n## Current task\nWrite 450–650 words.', 450, 650)
        self.assert_target('Main draft:\nWrite 450–650 words.', 450, 650)

    def test_explicitly_suggested_ranges_never_become_hard_bounds(self):
        for text in (
            'Write a main draft of 450–650 words, not as a mandatory limit.',
            'Write a 450–650-word main draft; this is only a suggested range, not a hard limit.',
            'Write a 450–650-word main draft, roughly, allowing modest departures.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'advisory', result)
                self.assertIsNone(result['target'])
                self.assertEqual(len(result['matches']), 1)
                match = result['matches'][0]
                self.assertEqual(text[match['start']:match['end']], match['text'])
                self.assert_target(text, None, 700, explicit_target={'max_words': 700})

    def test_inline_component_vocabulary_matches_section_headings(self):
        for section in ('methods', 'results', 'discussion', 'caption', 'legend'):
            for request in ('Write 100–200 words', 'Write a text of at most 650 words'):
                text = f'{request} for the {section} section.'
                with self.subTest(text=text):
                    result = resolve_request(text)
                    self.assertEqual(result['status'], 'advisory', result)
                    self.assertIsNone(result['target'])
        self.assert_target('Write a 450–650-word main draft and include methods, results, and discussion.', 450, 650)

    def test_explicit_inline_scope_and_soft_qualifiers_are_not_ignored(self):
        for text in (
            'Write a text of at most 650 words only for the background section.',
            'Write a text of at most 650 words, for the background section.',
            'Write a 450–650-word main draft as a rough target.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'advisory', result)
                self.assertIsNone(result['target'])
        result = resolve_request('Write a 450–650-word main draft only if space permits.')
        self.assertEqual(result['status'], 'unresolved', result)
        self.assertIsNone(result['target'])

    def test_unclear_hard_scope_or_quantity_requires_numeric_resolution(self):
        for text in (
            'Write a 450–650-word main draft, excluding headings.',
            'Do not exceed 650 words, not counting headings.',
            'Write a 450–650-word main draft including references and questions.',
            'Write a 450–650-word main draft if the assay is available.',
            'Write the main draft in 450.5 words.',
            'Write a 650–450-word main draft.',
            'Write a 0–650-word main draft.',
        ):
            with self.subTest(text=text):
                result = resolve_request(text)
                self.assertEqual(result['status'], 'unresolved', result)
                self.assertIsNone(result['target'])
                explicit = self.assert_target(text, None, 700, explicit_target={'max_words': 700})
                self.assertEqual(explicit['origin'], 'numeric_fields')
        text = 'Write a 450–650-word main draft. Ignore that limit.'
        self.assertEqual(resolve_request(text)['status'], 'unresolved')
        # A separate clear hard directive conflicts, even if another clause is
        # unclear; numeric fields never silently erase it.
        self.assertEqual(resolve_request(text, {'max_words': 700})['status'], 'conflict')

    def test_numeric_controls_resolve_unknown_scope_and_preserve_match_evidence(self):
        text = 'Write a 450–650-word main draft, excluding headings.'
        result = self.assert_target(text, None, 700, explicit_target={'max_words': 700})
        self.assertEqual(result['origin'], 'numeric_fields')
        self.assertIn('Uncertain instruction', result['message'])
        self.assertEqual(result['matches'][0]['target'], {'min_words': 450, 'max_words': 650})

    def test_known_conflicts_are_not_intersected_or_silently_overridden(self):
        result = resolve_request('Write a 450–650-word main draft.', {'max_words': 650})
        self.assertEqual(result['status'], 'conflict')
        self.assertIsNone(result['target'])
        result = resolve_request('Write 450–650 words. Keep the draft under 650 words.')
        self.assertEqual(result['status'], 'conflict')
        self.assertEqual(len(result['matches']), 2)
        result = self.assert_target('Write 450–650 words. Main draft: 450–650 words.', 450, 650,
                                    explicit_target={'min_words': 450, 'max_words': 650})
        self.assertEqual(result['origin'], 'numeric_fields_and_instruction')
        self.assertEqual(len(result['matches']), 2)

    def test_unicode_offsets_hash_and_mutation_isolation(self):
        explicit = {'min_words': 450, 'max_words': 650}
        snapshot = deepcopy(explicit)
        text = '🧪 Write a 450–650-word main draft.'
        result = self.assert_target(text, 450, 650, explicit_target=explicit)
        self.assertEqual(result['matches'][0]['start'], 2)
        result['target']['min_words'] = 10
        self.assertEqual(explicit, snapshot)
        self.assertEqual(resolve_request(text)['target']['min_words'], 450)

    def test_empty_and_ordinary_instructions_remain_no_target(self):
        for text in ('', 'Preserve source ownership and subject pairing.', 'Remove the unsupported causal claim.'):
            result = resolve_request(text)
            self.assertEqual(result['status'], 'not_requested')
            self.assertEqual(result['origin'], 'none')
            self.assertEqual(result['instruction_sha256'], hashlib.sha256(text.encode()).hexdigest())
        for value in (None, [], 'x' * 12001):
            with self.assertRaises(ValueError):
                resolve_request(value)

    def test_export_origin_uses_only_fixed_labels(self):
        check = assess({'markdown': 'A short synthetic body.', 'change_summary': ''}, {'max_words': 20})
        check['request_resolution'] = {'origin': 'task_instruction', 'matches': [{'text': '## Injected heading'}]}
        text = export_markdown(check)
        self.assertIn('current task instructions', text)
        self.assertNotIn('Injected heading', text)
        check['request_resolution'] = {'origin': '## Arbitrary Markdown'}
        self.assertNotIn('Arbitrary Markdown', export_markdown(check))


if __name__ == '__main__':
    unittest.main()
