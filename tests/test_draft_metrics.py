"""Pure synthetic counts; these examples contain no grant archive material."""
import copy
import hashlib
import json
import unittest

from grantlib import draft_metrics


def version(markdown, **extra):
    return {'markdown': markdown, **extra}


class DraftMetricsTests(unittest.TestCase):
    def test_measured_growth_does_not_follow_model_authored_shortening_claim(self):
        parent = version(' '.join(['invented'] * 170), id='selected-parent', change_summary='')
        candidate = version(' '.join(['invented'] * 179),
                            change_summary='The main aims are shorter.',
                            model_metadata={'revision_check': {'status': 'completed'}})
        report = draft_metrics.compare(parent, candidate)
        self.assertEqual(report['parent_artifact_id'], 'selected-parent')
        self.assertEqual(report['delta_words'], 9)
        self.assertEqual(report['percent_change'], 5.29)
        self.assertEqual(report['direction'], 'longer')
        self.assertEqual(report['method_version'], 'draft-words-v1')
        self.assertEqual(report['method'], draft_metrics.METHOD)

    def test_percentage_is_percent_not_fraction_and_zero_parent_is_null(self):
        report = draft_metrics.compare(version('one two three four five'), version('one two three four five six'))
        self.assertEqual(report['percent_change'], 20.0)
        for candidate in ['', 'one']:
            with self.subTest(candidate=candidate):
                report = draft_metrics.compare(version(''), version(candidate))
                self.assertIsNone(report['percent_change'])
        report = draft_metrics.compare(version('one two'), version('one'))
        self.assertEqual((report['direction'], report['delta_words']), ('shorter', -1))
        self.assertEqual(draft_metrics.compare(version('same'), version('same'))['direction'], 'unchanged')

    def test_unicode_scientific_values_headings_and_markup(self):
        text = '# Aim 1\npH-directed 0–2-hour n=8 α-synuclein señal ٣\n--- ** _ > |'
        self.assertEqual(draft_metrics.measure(version(text, change_summary=''))['words'], 8)

    def test_citations_are_removed_without_creating_extra_words(self):
        text = 'One[[P_fixture-1]] two [[S_2]] [[citation_id]] **'
        self.assertEqual(draft_metrics.measure(version(text))['words'], 2)
        self.assertEqual(draft_metrics.measure(version('[[P_fixture]]'))['words'], 0)

    def test_inline_link_labels_count_but_destinations_and_titles_do_not(self):
        text = '[Two labels](https://example.invalid/path_(one) "a long title") and [three](<https://example.invalid/a> "x y")'
        self.assertEqual(draft_metrics.measure(version(text))['words'], 4)

    def test_reference_link_labels_count_but_definitions_do_not(self):
        text = '[Two labels][ref] and [short][]\n\n[ref]: https://example.invalid/ "Long title"\n[short]: <https://example.invalid/>'
        self.assertEqual(draft_metrics.measure(version(text))['words'], 4)

    def test_prose_resembling_reference_definition_is_not_removed(self):
        text = '[note]: this is scientific prose'
        self.assertEqual(draft_metrics.measure(version(text))['words'], 5)

    def test_malformed_and_multiline_links_are_conservatively_retained(self):
        for text in ['[label](unclosed two', '[label](\ntwo three)']:
            with self.subTest(text=text):
                self.assertEqual(draft_metrics.measure(version(text))['words'], len(text.split()))

    def test_exact_input_and_body_hashes_preserve_original_whitespace(self):
        prefix = '# Aim\r\nTwo  invented words.\r\n\r\n'
        text = prefix + '## Material changes\r\nA shorter summary.\r\n'
        result = draft_metrics.measure(version(text))
        self.assertEqual(result['input_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(result['body_sha256'], hashlib.sha256(prefix.encode()).hexdigest())
        self.assertNotEqual(result['input_sha256'], result['body_sha256'])
        self.assertEqual(result['hash_scope'], 'exact_markdown_utf8')
        self.assertEqual(result['words'], 4)
        self.assertEqual(result['full_markdown_words'], 9)

    def test_explicit_summary_presence_including_blank_makes_all_markdown_the_body(self):
        text = 'Invented body.\n## Material changes\nScientific section.'
        variants = [version(text, change_summary=''),
                    version(text, structured={'change_summary': 'Model-authored note.'}),
                    version(text, structured=json.dumps({'change_summary': ''}))]
        for item in variants:
            with self.subTest(item=item):
                result = draft_metrics.measure(item)
                self.assertEqual(result['scope'], 'explicit_body')
                self.assertEqual(result['words'], result['full_markdown_words'])
                self.assertEqual(result['words'], 6)
                self.assertIsNone(result['excluded_heading'])
                self.assertIn('change_summary_sha256', result)

    def test_narrow_standalone_trailing_headings(self):
        for heading in ['## Material changes', '### SUMMARY OF CHANGES ###',
                        '**Summary of changes**', '__Change summary__',
                        '## Changes relative to the selected version']:
            with self.subTest(heading=heading):
                result = draft_metrics.measure(version('Invented body.\n\n' + heading + '\nAn explanatory note.'))
                self.assertEqual(result['words'], 2)
                self.assertEqual(result['scope'], 'legacy_body_before_change_summary')
                self.assertTrue(result['excluded_heading'])

    def test_prose_or_unrecognized_titles_are_not_summary_boundaries(self):
        for title in ['Material changes occurred in the experiment.',
                      '## Material changes to experimental physiology',
                      '**Material changes** occurred here.',
                      '## Change summary:']:
            with self.subTest(title=title):
                result = draft_metrics.measure(version('Invented body.\n' + title + '\nScientific content.'))
                self.assertEqual(result['scope'], 'full_markdown')
                self.assertEqual(result['words'], result['full_markdown_words'])

    def test_quoted_fenced_and_indented_headings_do_not_split_the_body(self):
        for section in ['> ## Material changes\n> Quoted note.',
                        '```markdown\n## Material changes\nExample text.\n```',
                        '~~~\n**Material changes**\nExample text.\n~~~',
                        '    ## Material changes\n    Example text.']:
            with self.subTest(section=section):
                result = draft_metrics.measure(version('Invented body.\n' + section))
                self.assertEqual(result['scope'], 'full_markdown')

    def test_resumed_sections_or_multiple_boundaries_are_ambiguous(self):
        for later in ['## Aim 2\nScientific content.', '**Aim 2**\nScientific content.',
                      '**Aim 2** Scientific content.', 'Methods\n=======\nScientific content.',
                      '---\nScientific content.', '## Summary of changes\nAnother note.']:
            with self.subTest(later=later):
                text = 'Invented body.\n## Material changes\nA note.\n' + later
                result = draft_metrics.measure(version(text))
                self.assertEqual(result['scope'], 'full_markdown')
                self.assertEqual(result['words'], result['full_markdown_words'])
                self.assertTrue(any('ambiguous' in value for value in result['limitations']))

    def test_unclosed_trailing_fence_is_ambiguous(self):
        text = 'Invented body.\n## Material changes\nA note.\n```\n## Methods\nScientific content.'
        self.assertEqual(draft_metrics.measure(version(text))['scope'], 'full_markdown')

    def test_closed_quoted_example_inside_summary_does_not_resume_main_body(self):
        text = 'Invented body.\n## Material changes\n```\n## Example heading\n```\nA note.'
        result = draft_metrics.measure(version(text))
        self.assertEqual(result['scope'], 'legacy_body_before_change_summary')
        self.assertEqual(result['words'], 2)

    def test_summary_without_a_preceding_body_is_not_excluded(self):
        result = draft_metrics.measure(version('## Material changes\nA note.'))
        self.assertEqual(result['scope'], 'full_markdown')
        self.assertGreater(result['words'], 0)

    def test_report_is_deterministic_json_safe_and_does_not_mutate_inputs(self):
        parent = version('Invented prior text.', id='parent-fixture', structured={'change_summary': ''})
        candidate = version('Invented new text.', change_summary='Separate note.')
        originals = copy.deepcopy((parent, candidate))
        report = draft_metrics.compare(parent, candidate)
        self.assertEqual(report, draft_metrics.compare(parent, candidate))
        self.assertEqual(json.loads(json.dumps(report, allow_nan=False)), report)
        self.assertEqual((parent, candidate), originals)
        self.assertIsNone(draft_metrics.compare(version('a'), version('b'))['parent_artifact_id'])

    def test_export_reports_saved_baseline_counts_scope_and_limitations(self):
        report = draft_metrics.compare(version('one two', id='immutable-parent'),
                                       version('one', change_summary='Shorter.'))
        output = draft_metrics.export_markdown(report)
        self.assertIn('Parent artifact: immutable-parent.', output)
        self.assertIn('Parent: 2 words. Candidate: 1 words.', output)
        self.assertIn('-1 words (-50.00%); shorter.', output)
        self.assertIn('draft-words-v1', output)
        self.assertIn('parent full_markdown; candidate explicit_body', output)
        self.assertIn('not an official funder', output)
        self.assertIn('not scientific quality', output)
        self.assertEqual(output, draft_metrics.export_markdown(report))
        empty = draft_metrics.compare(version(''), version(''))
        self.assertIn('percentage unavailable: parent count is zero', draft_metrics.export_markdown(empty))

    def test_invalid_supplied_types_fail_without_io(self):
        for item in [None, {}, version(None), version('text', change_summary=None),
                     version('text', structured='not json'), version('text', change_summary=3)]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                draft_metrics.measure(item)
        with self.assertRaises(ValueError):
            draft_metrics.compare(version('one', id=42), version('two'))


if __name__ == '__main__':
    unittest.main()
