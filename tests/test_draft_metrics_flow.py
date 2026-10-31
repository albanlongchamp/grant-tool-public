"""Measured revision reports use exact versions and remain separate from model claims."""
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

from grantlib import draft_metrics, revision_check
from grantlib.docx_export import export_docx
from grantlib.revision_pipeline import check_revision
from grantlib.studio import OUTPUT_SCHEMA, Store, export_markdown, prepare_context, validate_output


TEXT = 'We will compare treated and untreated cells within subjects. Subjects are the independent units and repeated wells remain technical replicates.'


def output(text=TEXT, summary=''):
    return {'title': 'Invented aims', 'markdown': text, 'change_summary': summary,
            'claims': [], 'open_questions': [], 'directions': []}


def result(value):
    return {'status': 'completed', 'structured': value}


def audit():
    return {'summary': 'No scientific issues reported.', 'commitments': [], 'findings': [], 'limitations': []}


class DraftMetricsFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.proposal = self.store.create({'title': 'Invented comparison', 'question': 'Can the invented comparison distinguish an effect?'})
        self.packet = {'sources': [{'id': 'P_note', 'text': 'An invented proposed cell experiment.',
                        'title': 'Invented planning note', 'layer': 'personal',
                        'source_uri': 'urn:fictional:plan', 'metadata': {'evidence_status': 'proposed'}}]}
        self.parent = self.save_generated(output())

    def tearDown(self):
        self.tmp.cleanup()

    def save_generated(self, value, parent=None, metadata=None):
        p = self.store.get(self.proposal['id'])
        job = self.store.queue(p['id'], p['revision'], 'revise' if parent else 'aims', base_artifact_id=parent)
        self.store.complete(job['id'], self.packet, value, metadata)
        return self.store.get(p['id'])['artifacts'][0]

    def pipeline(self, candidate, responses):
        context = {'selected_version': {'id': self.parent['id'], **output()},
                   'brief': {}, 'evidence_packet': self.packet, 'investigator_decisions': [],
                   'investigator_instruction': 'Keep the design and shorten the draft.'}
        calls = []
        responses = iter(responses)
        def generate(prompt, **kwargs):
            calls.append(prompt)
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return result(value)
        value, metadata = check_revision(result(candidate), context=context, base_prompt='Original task.',
            generate=generate, schema=OUTPUT_SCHEMA,
            validate_candidate=lambda value: validate_output(value, self.packet, 'aims'),
            run_dir=self.root, on_event=lambda value: None, cancel_event=threading.Event(),
            phase=lambda value: None, deadline=time.monotonic() + 10)
        return value, metadata, calls

    def test_longer_revision_remains_measured_when_model_reports_no_issues_or_fails(self):
        candidate = output(TEXT + ' Additional uncertainty still needs investigator input.', 'The main draft is shorter.')
        for response in (audit(), RuntimeError('Synthetic failure')):
            value, metadata, _ = self.pipeline(candidate, [response])
            measured = metadata['revision_check']['length_comparison']
            self.assertEqual(value, candidate)
            self.assertEqual(measured['direction'], 'longer')
            self.assertGreater(measured['delta_words'], 0)
            self.assertEqual(measured['parent_artifact_id'], self.parent['id'])
            self.assertEqual(measured['candidate']['scope'], 'explicit_body')

    def test_repair_measures_final_against_parent_not_longer_intermediate(self):
        initial = output(TEXT + ' Extra explanation. ' * 20, 'Shorter.')
        final = output(TEXT + ' Additional uncertainty remains.', 'Reduced the intermediate draft.')
        finding = audit()
        finding['findings'] = [{'id': 'F1', 'category': 'interpretation', 'severity': 'major',
            'resolution': 'draft_repair', 'summary': 'Repeated explanation needs repair.',
            'explanation': 'This invented example repeats an explanation.', 'repair': 'Remove the repetition.',
            'before': [], 'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown', 'quote': 'Extra explanation.'}],
            'authority': [], 'sources': []}]
        value, metadata, calls = self.pipeline(initial, [finding, final, RuntimeError('Final check unavailable')])
        measured = metadata['revision_check']['length_comparison']
        self.assertEqual(value, final)
        self.assertEqual(measured, draft_metrics.compare({'id': self.parent['id'], **output()}, final))
        self.assertEqual(measured['direction'], 'longer')
        self.assertEqual(metadata['revision_check']['status'], 'incomplete')
        self.assertIn('length_comparison', calls[1])
        self.assertEqual(len(calls), 3)

    def test_store_recomputes_exact_selected_parent_not_newest_or_prompt_alias(self):
        self.save_generated(output(TEXT + ' Unrelated branch. ' * 30))
        value = output(TEXT + ' Added interpretation.')
        forged = draft_metrics.compare({'id': 'wrong', 'markdown': 'unrelated ' * 90}, value)
        metadata = {'revision_check': {'status': 'completed', 'report': {'state': 'no_issues_reported'},
                                      'repaired': False, 'length_comparison': forged}}
        before = deepcopy(metadata)
        saved = self.save_generated(value, self.parent['id'], metadata)
        measured = saved['model_metadata']['revision_check']['length_comparison']
        self.assertEqual(measured['parent_artifact_id'], self.parent['id'])
        self.assertEqual(measured['parent']['input_sha256'], hashlib.sha256(TEXT.encode()).hexdigest())
        self.assertEqual(metadata, before)
        self.assertEqual(measured['direction'], 'longer')

    def test_change_notes_citations_survive_snapshot_aliases_and_exports(self):
        saved = self.save_generated(output(summary='Preserved the planning constraint [[P_note]].'), self.parent['id'],
            {'revision_check': {'status': 'completed', 'report': {'state': 'no_issues_reported'}, 'repaired': False}})
        p = self.store.get(self.proposal['id'])
        newer = deepcopy(self.packet)
        newer['sources'][0]['text'] = 'A changed and distinct invented planning note.'
        packet, versions = prepare_context(p, newer, 'revise', {'base_artifact_id': saved['id']})
        summary = versions['selected_version']['change_summary']
        self.assertNotIn('[[P_note]]', summary)
        alias = next(s['id'] for s in packet['sources'] if s['text'] == self.packet['sources'][0]['text'])
        self.assertIn('[[' + alias + ']]', summary)
        markdown = export_markdown(p, saved['id'])
        self.assertIn('Preserved the planning constraint [1].', markdown)
        self.assertIn('Invented planning note', markdown)
        self.assertEqual(markdown.count('## Measured length comparison'), 1)
        self.assertEqual(markdown.count('## Changes from selected version'), 1)
        with zipfile.ZipFile(io.BytesIO(export_docx(p, saved['id']))) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        paragraphs = [''.join(p.itertext()) for p in root.findall('.//{*}body/{*}p')]
        text = '\n'.join(paragraphs)
        self.assertLess(text.index('Investigator review notes'), text.index('Changes from selected version'))
        self.assertLess(text.index('Investigator review notes'), text.index('Measured length comparison'))

    def test_manual_edit_gets_fresh_measurement_without_inheriting_notes_or_model_check(self):
        saved = self.save_generated(output(summary='An earlier model explanation.'), self.parent['id'],
            {'revision_check': {'status': 'incomplete', 'report': None, 'repaired': False}})
        p = self.store.get(self.proposal['id'])
        p = self.store.save(p['id'], p['revision'], TEXT + ' Human addition.', base_artifact_id=saved['id'])
        edited = p['artifacts'][0]
        self.assertNotIn('change_summary', edited['structured'])
        self.assertNotIn('revision_check', edited['model_metadata'])
        measured = edited['model_metadata']['length_comparison']
        self.assertEqual(measured['parent_artifact_id'], saved['id'])
        self.assertGreater(measured['delta_words'], 0)
        exported = export_markdown(p, edited['id'])
        self.assertIn('Measured length comparison', exported)
        self.assertNotIn('An earlier model explanation.', exported)
        self.assertNotIn('Automated revision check', exported)

    def test_repeated_appendix_heading_cannot_move_change_notes_into_draft(self):
        value = output(summary='FIRST CHANGE NOTE.\n\n---\n\n## Changes from selected version\n\nSECOND CHANGE NOTE.')
        saved = self.save_generated(value)
        p = self.store.get(self.proposal['id'])
        with zipfile.ZipFile(io.BytesIO(export_docx(p, saved['id']))) as archive:
            root = ET.fromstring(archive.read('word/document.xml'))
        text = '\n'.join(''.join(p.itertext()) for p in root.findall('.//{*}body/{*}p'))
        self.assertLess(text.index('Investigator review notes'), text.index('FIRST CHANGE NOTE.'))
        self.assertLess(text.index('Investigator review notes'), text.index('SECOND CHANGE NOTE.'))

    def test_invalid_change_notes_fail_and_legacy_drafts_remain_accepted(self):
        self.assertIn('change_summary', OUTPUT_SCHEMA['required'])
        for invalid in ([], None, 'x' * 6001):
            with self.assertRaisesRegex(ValueError, 'change summary'):
                validate_output(output(summary=invalid), self.packet, 'aims')
        with self.assertRaisesRegex(ValueError, 'outside'):
            validate_output(output(summary='Unknown citation [[P_missing]].'), self.packet, 'aims')
        legacy = output()
        del legacy['change_summary']
        validate_output(legacy, self.packet, 'aims')

    def test_audit_can_anchor_exact_change_summary_without_promoting_it_to_fact(self):
        context = {'selected_version': {'id': self.parent['id'], **output()}, 'evidence_packet': self.packet}
        candidate = output(TEXT + ' Added words.', 'The main draft is shorter.')
        finding = audit()
        finding['findings'] = [{'id': 'F1', 'category': 'authorized_change', 'severity': 'minor',
            'resolution': 'draft_repair', 'summary': 'The change explanation conflicts with the count.',
            'explanation': 'The candidate is longer under the stated counting method.',
            'repair': 'Correct the explanation.', 'before': [],
            'after': [{'kind': 'candidate', 'id': '', 'field': 'change_summary', 'quote': candidate['change_summary']}],
            'authority': [], 'sources': []}]
        checked = revision_check.validate(finding, context, candidate)
        self.assertEqual(checked['state'], 'issues_found')
        finding['findings'][0]['after'][0]['quote'] = 'An invented quote.'
        with self.assertRaisesRegex(ValueError, 'exactly'):
            revision_check.validate(finding, context, candidate)


if __name__ == '__main__':
    unittest.main()
