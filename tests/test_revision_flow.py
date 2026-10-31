"""Checks remain attached to the exact saved revision and evidence snapshot."""
from copy import deepcopy
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

from grantlib.docx_export import export_docx
from grantlib.revision_check import validate
from grantlib.studio import Service, Store, export_markdown, prepare_context


TEXT = 'We will allocate conditions within each processing batch. Subjects are the independent units and wells are technical replicates.'
SOURCE = 'Proposed experiment: [[literal_token]]\n**No completed result.**'


def draft():
    return {'title': 'Synthetic draft', 'markdown': TEXT, 'claims': [], 'open_questions': [], 'directions': []}


def clean_report():
    return {'summary': 'No additional issue was identified.', 'commitments': [], 'findings': [], 'limitations': []}


class RevisionFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.p = self.store.create({'title': 'Invented cell experiment', 'question': 'What can the proposed comparison establish?'})
        self.packet = {'sources': [{'id': 'P_plan', 'layer': 'personal', 'title': 'Invented unverified plan',
                       'text': SOURCE, 'source_uri': 'urn:fictional:plan',
                       'metadata': {'role': 'collaborator', 'evidence_status': 'proposed', 'locator': 'Paragraph 1'}}]}
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims')
        self.store.complete(job['id'], self.packet, draft())
        self.p = self.store.get(self.p['id'])
        self.parent = self.p['artifacts'][0]

    def tearDown(self):
        self.tmp.cleanup()

    def wait(self, service, job):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = service.store.job(job['id'])
            if state['status'] not in ('queued', 'running'):
                return state
            time.sleep(.01)
        self.fail('Synthetic service did not reach a terminal state')

    def test_live_service_saves_exact_check_and_manual_edit_does_not_inherit_it(self):
        calls = []
        def generate(prompt, **kwargs):
            calls.append(kwargs['run_dir'].name)
            return {'status': 'completed', 'structured': clean_report() if calls[-1] == 'check-initial' else draft()}
        service = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: self.packet)
        job = service.start(proposal_id=self.p['id'], expected_revision=self.p['revision'], action='revise', base_artifact_id=self.parent['id'])
        self.assertEqual(self.wait(service, job)['status'], 'completed')
        self.assertEqual(calls, ['draft', 'check-initial'])
        revised = self.store.get(self.p['id'])
        checked = revised['artifacts'][0]
        self.assertEqual(checked['model_metadata']['revision_check']['parent_artifact_id'], self.parent['id'])
        self.assertEqual(checked['model_metadata']['revision_check']['status'], 'completed')
        self.assertEqual(revised['artifacts'][1], self.parent)
        edited = self.store.save(revised['id'], revised['revision'], TEXT + ' Edited wording.', base_artifact_id=checked['id'])
        self.assertNotIn('revision_check', edited['artifacts'][0]['model_metadata'])
        self.assertIn('revision_check', edited['artifacts'][1]['model_metadata'])

    def test_cancellation_during_check_does_not_save_draft(self):
        entered, release = threading.Event(), threading.Event()
        def generate(prompt, **kwargs):
            if kwargs['run_dir'].name == 'check-initial':
                entered.set()
                release.wait(3)
                return {'status': 'completed', 'structured': clean_report()}
            return {'status': 'completed', 'structured': draft()}
        service = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: self.packet)
        job = service.start(proposal_id=self.p['id'], expected_revision=self.p['revision'], action='revise', base_artifact_id=self.parent['id'])
        self.assertTrue(entered.wait(2))
        service.cancel(job['id'])
        release.set()
        self.assertEqual(self.wait(service, job)['status'], 'cancelled')
        self.assertEqual(self.store.get(self.p['id'])['artifacts'], [self.parent])

    def checked_version(self):
        context = {'selected_version': {'id': self.parent['id'], **draft()}, 'brief': self.p['brief'],
                   'evidence_packet': self.packet, 'investigator_decisions': [], 'investigator_instruction': ''}
        raw = clean_report()
        raw['summary'] = 'Source ownership remains unresolved before reuse.'
        raw['findings'] = [{'id': 'ownership', 'category': 'source_fidelity', 'severity': 'minor',
                           'resolution': 'investigator_input', 'summary': 'Confirm source ownership before reuse.',
                           'explanation': 'This is an unverified collaborator plan.', 'repair': 'Ask the investigator.',
                           'before': [], 'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown', 'quote': TEXT}],
                           'authority': [], 'sources': [{'kind': 'source', 'id': 'P_plan', 'field': 'text', 'quote': SOURCE}]}]
        report = validate(raw, context, draft())
        job = self.store.queue(self.p['id'], self.p['revision'], 'revise', base_artifact_id=self.parent['id'])
        self.store.complete(job['id'], self.packet, draft(), {'revision_check': {'status': 'completed', 'report': report, 'repaired': False}})
        return self.store.get(self.p['id'])

    def test_audit_only_source_is_retained_and_literal_quotes_survive_changed_snapshot(self):
        p = self.checked_version()
        saved = deepcopy(p)
        updated = deepcopy(self.packet)
        updated['sources'][0]['text'] = 'A later and different source snapshot.'
        packet, versions = prepare_context(p, updated, 'revise', {'base_artifact_id': p['artifacts'][0]['id']})
        check = versions['selected_version']['previous_revision_check']
        anchor = check['report']['findings'][0]['sources'][0]
        self.assertNotEqual(anchor['id'], 'P_plan')
        self.assertEqual(anchor['quote'], SOURCE)
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == anchor['id'])['text'], SOURCE)
        self.assertEqual(p, saved)
        _, fresh_review = prepare_context(p, self.packet, 'review', {'base_artifact_id': p['artifacts'][0]['id']})
        self.assertNotIn('previous_revision_check', fresh_review['selected_version'])

    def test_markdown_and_word_keep_audit_in_appendix_and_preserve_source_scope(self):
        p = self.checked_version()
        artifact = p['artifacts'][0]
        markdown = export_markdown(p, artifact['id'])
        self.assertEqual(markdown.count('## Automated revision check'), 1)
        self.assertIn(SOURCE, markdown)
        self.assertIn('collaborator / proposed', markdown)
        self.assertNotIn('Unresolved source: literal_token', markdown)
        with zipfile.ZipFile(io.BytesIO(export_docx(p, artifact['id']))) as package:
            root = ET.fromstring(package.read('word/document.xml'))
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        paragraphs = [''.join(node.itertext()) for node in root.findall('.//w:body/w:p', ns)]
        combined = '\n'.join(paragraphs)
        self.assertLess(combined.index('Investigator review notes'), combined.index('Automated revision check'))
        self.assertIn('[[literal_token]]', combined)
        self.assertIn('**No completed result.**', combined)


if __name__ == '__main__':
    unittest.main()
