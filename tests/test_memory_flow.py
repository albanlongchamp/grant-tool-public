"""Reusable knowledge enters only chosen proposals and stays versioned."""
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
import zipfile

from grantlib import memory, claim_review
from grantlib import revision_check
from grantlib.docx_export import export_docx
from grantlib.studio import Service, Store, export_markdown, prepare_context, check_citations


def record(**updates):
    value = {'category': 'capability', 'text': 'The invented core supports masked imaging. [[literal_note]]',
             'scope': 'Invented cell cultures only.', 'tags': ['imaging'],
             'provenance_note': 'Fictional investigator confirmation for a software test.',
             'review_after': '', 'status': 'confirmed'}
    value.update(updates)
    return value


def draft(source_id=None):
    text = 'The proposed experiment will preserve subject pairing and uncertainty-aware interpretation; the biological mechanism and quantitative assumptions remain investigator inputs.'
    if source_id:
        text += ' The investigator reports imaging access [[' + source_id + ']].'
    return {'title': 'Invented aims', 'markdown': text, 'claims': [], 'open_questions': [], 'directions': []}


class MemoryFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = Store(self.root)
        self.p = self.store.create({'title': 'Invented grant', 'question': 'What can a paired experiment establish?'})

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, **updates):
        catalog = self.store.create_memory(self.store.memory_catalog()['revision'], record(**updates), 'investigator')
        return catalog['entries'][0]

    def select(self, item, note='Scope is limited to the invented cell-culture aim.'):
        p = self.store.get(self.p['id'])
        return self.store.select_memory(p['id'], p['revision'], self.store.memory_catalog()['revision'],
            [{'entry_id': item['id'], 'version_id': item['version_id'], 'note': note}] if item else [])

    def wait(self, service, job):
        until = time.monotonic() + 5
        while time.monotonic() < until:
            state = service.store.job(job['id'])
            if state['status'] not in ('queued', 'running'):
                return state
            time.sleep(.01)
        self.fail('Invented job did not finish')

    def generate(self, callback=None):
        captured = []
        def generate(prompt, **kwargs):
            data = json.loads(prompt.split('PROPOSAL AND SOURCE DATA (JSON):\n')[1])
            captured.append(data)
            if callback:
                callback()
            sources = data['evidence_packet']['sources']
            source_id = next((s['id'] for s in sources if s['layer'] == 'investigator_memory'), None)
            return {'status': 'completed', 'structured': draft(source_id), 'provider': 'injected'}
        service = Service(self.root / 'library.sqlite3', generate=generate, packet_builder=lambda *a: {'sources': []})
        p = self.store.get(self.p['id'])
        job = service.start(proposal_id=p['id'], expected_revision=p['revision'], action='aims')
        return self.wait(service, job), captured

    def test_catalog_is_not_sent_without_proposal_selection(self):
        item = self.add()
        self.add(status='proposed', text='An unconfirmed sequencing facility.')
        job, inputs = self.generate()
        self.assertEqual(job['status'], 'completed')
        self.assertNotIn(item['text'], json.dumps(inputs))
        self.assertEqual(inputs[0]['evidence_packet']['selected_memory'], [])
        other = self.store.create({'title': 'Other invented grant', 'question': 'Another unrelated question'})
        self.select(item)
        self.assertEqual(self.store.get(other['id'])['memory_bindings'], [])

    def test_selected_memory_is_frozen_in_prompt_and_artifact_with_literal_text(self):
        item = self.add()
        self.select(item)
        job, inputs = self.generate()
        self.assertEqual(job['status'], 'completed')
        data = inputs[0]['evidence_packet']
        self.assertEqual(data['selected_memory'][0]['text'], item['text'])
        self.assertEqual(data['sources'][0]['text'], item['text'])
        p = self.store.get(self.p['id'])
        artifact = p['artifacts'][0]
        self.assertEqual(artifact['model_metadata']['selected_memory'], data['selected_memory'])
        markdown = export_markdown(p, artifact['id'])
        self.assertIn('Investigator memory selected for this version', markdown)
        self.assertIn(item['text'], markdown)
        self.assertIn(item['scope'], markdown)
        self.assertIn(item['provenance_note'], markdown)
        self.assertNotIn('Unresolved source: literal_note', markdown)
        with zipfile.ZipFile(BytesIO(export_docx(p, artifact['id']))) as z:
            root = ET.fromstring(z.read('word/document.xml'))
        text = '\n'.join(root.itertext())
        self.assertLess(text.index('Investigator review notes'), text.index('Investigator memory selected'))
        self.assertIn('[[literal_note]]', text)
        check = check_citations({'markdown': '', 'claims': [{'basis': 'source_report', 'source_ids': [data['sources'][0]['id']]}]}, data)
        self.assertTrue(any('does not independently establish' in w for w in check['warnings']))

    def test_retirement_before_dispatch_prevents_any_model_request(self):
        item = self.add()
        p = self.select(item)
        job = self.store.queue(p['id'], p['revision'], 'aims')
        self.store.revise_memory(item['id'], self.store.memory_catalog()['revision'], record(status='retired'), 'investigator')
        calls = []
        service = Service(self.root / 'library.sqlite3', generate=lambda *a, **kw: calls.append(a), packet_builder=lambda *a: {'sources': []})
        service._execute(job['id'], threading.Event())
        self.assertEqual(calls, [])
        self.assertEqual(self.store.job(job['id'])['status'], 'failed')
        self.assertEqual(self.store.get(p['id'])['artifacts'], [])

    def test_memory_claim_judgment_retains_the_limits_of_investigator_confirmation(self):
        item = self.add()
        self.select(item)
        self.generate()
        p = self.store.get(self.p['id'])
        artifact = p['artifacts'][0]
        p = self.store.add_claim(p['id'], p['revision'], artifact['id'], 'The investigator reports imaging access')
        claim = p['artifacts'][0]['claims'][0]
        p = self.store.assess_claim(p['id'], p['revision'], artifact['id'], claim['id'],
            source_id='M_' + item['version_id'], verdict='supported', excerpt=item['text'],
            locator='Recorded memory statement', note='Supports the attributed statement within its scope only.', reviewer='assistant')
        assessment = p['artifacts'][0]['claims'][0]['assessments'][0]
        self.assertIn('not independent research evidence', ' '.join(assessment['warnings']))
        self.assertEqual(assessment['source_snapshot']['metadata']['scope'], item['scope'])
        notes = claim_review.export_reviews(p['artifacts'][0])
        self.assertIn(item['confirmed_at'], notes)
        self.assertIn(item['provenance_note'], notes)
        self.assertIn(item['scope'], notes)

    def test_audit_only_memory_source_exports_its_confirmation_and_basis(self):
        item = self.add()
        p = self.select(item)
        with self.store.connect() as c:
            selected = memory.snapshot(c, p['id'])[0]
        source = memory.source(selected)
        parent = {'id': 'a' * 32, **draft()}
        raw = {'summary': 'A dated memory statement needs context.', 'commitments': [], 'limitations': [],
               'findings': [{'id': 'memory-scope', 'category': 'missing_input', 'severity': 'minor',
                 'resolution': 'investigator_input', 'summary': 'Review applicability before reuse.',
                 'explanation': 'This source records an investigator statement only.', 'repair': 'Confirm its relevant scope.',
                 'before': [{'kind': 'parent', 'id': parent['id'], 'field': 'markdown', 'quote': parent['markdown']}],
                 'after': [], 'authority': [],
                 'sources': [{'kind': 'source', 'id': source['id'], 'field': 'text', 'quote': source['text']}]}]}
        report = revision_check.validate(raw, {'selected_version': parent, 'evidence_packet': {'sources': [source]}}, draft())
        text = revision_check.export_markdown(report)
        self.assertIn(item['confirmed_at'], text)
        self.assertIn(item['provenance_note'], text)
        self.assertIn(item['scope'], text)
        self.assertIn('not independent research evidence', text)

    def test_selected_record_changed_during_generation_cannot_save_stale_draft(self):
        item = self.add()
        self.select(item)
        def retire():
            self.store.revise_memory(item['id'], self.store.memory_catalog()['revision'], record(status='retired'), 'investigator')
        job, inputs = self.generate(retire)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(len(inputs), 1)
        self.assertEqual(self.store.get(self.p['id'])['artifacts'], [])
        self.assertIn('retired', job['error'])

    def test_unrelated_catalog_edit_during_generation_does_not_discard_draft(self):
        item = self.add()
        self.select(item)
        job, _ = self.generate(lambda: self.add(category='writing_preference', text='An unrelated fictional preference.'))
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(self.store.get(self.p['id'])['artifacts'][0]['model_metadata']['selected_memory'][0]['version_id'], item['version_id'])

    def test_reselection_and_removal_distinguish_historical_citations_from_current_memory(self):
        old = self.add()
        self.select(old)
        self.generate()
        original = self.store.get(self.p['id'])['artifacts'][0]
        catalog = self.store.revise_memory(old['id'], self.store.memory_catalog()['revision'], record(text='Only one invented imaging session remains.'), 'investigator')
        new = catalog['entries'][0]
        p = self.select(new)
        job = self.store.queue(p['id'], p['revision'], 'revise', base_artifact_id=original['id'])
        payload = self.store.job(job['id'], True)['payload']
        packet, _ = prepare_context(p, original['context'], 'revise', payload)
        sources = {s['id']: s for s in packet['sources']}
        self.assertEqual(sources['M_' + old['version_id']]['text'], old['text'])
        self.assertEqual(sources['M_' + new['version_id']]['text'], new['text'])
        self.assertEqual([m['version_id'] for m in packet['selected_memory']], [new['version_id']])
        self.assertEqual(self.store.get(p['id'])['artifacts'][0], original)
        self.store.terminal(job['id'], 'cancelled', 'End injected test')
        p = self.select(None)
        packet, _ = prepare_context(p, original['context'], 'design', {'memory_snapshot': []})
        self.assertEqual(packet['selected_memory'], [])
        self.assertFalse(any(s['layer'] == 'investigator_memory' for s in packet['sources']))

    def test_manual_new_citation_adds_selected_memory_without_mutating_parent_context(self):
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims')
        self.store.complete(job['id'], {'sources': []}, draft())
        parent = self.store.get(self.p['id'])['artifacts'][0]
        item = self.add()
        p = self.select(item)
        p = self.store.save(p['id'], p['revision'], draft('M_' + item['version_id'])['markdown'], base_artifact_id=parent['id'], origin='assistant_import')
        self.assertEqual(p['artifacts'][1], parent)
        self.assertEqual(p['artifacts'][0]['context']['sources'][0]['text'], item['text'])
        self.assertEqual(p['artifacts'][0]['citation_check']['unknown_ids'], [])
        exported = export_markdown(p, p['artifacts'][0]['id'])
        self.assertIn(item['text'], exported)
        self.assertIn(item['scope'], exported)
        self.assertIn(item['provenance_note'], exported)
        self.assertIn(item['confirmed_at'], exported)
        self.assertNotIn('Investigator memory selected for this version', exported)

    def test_cli_requires_explicit_investigator_attribution_for_confirmation(self):
        config = self.root / 'config.json'
        config.write_text(json.dumps({'data_dir': str(self.root),
                                     'sources': [{'name': 'invented', 'path': str(self.root / 'unopened-sources')}]}))
        data = self.root / 'memory.json'
        data.write_text(json.dumps(record()))
        command = [sys.executable, '-m', 'grantlib', '--config', str(config), 'memory', 'create',
                   '--file', str(data), '--expected-catalog-revision', '1']
        denied = subprocess.run(command, capture_output=True, text=True, timeout=10)
        self.assertEqual(denied.returncode, 1)
        self.assertIn('Only an explicit investigator', denied.stderr)
        approved = subprocess.run(command + ['--actor', 'investigator'], capture_output=True, text=True, timeout=10)
        self.assertEqual(approved.returncode, 0, approved.stderr)
        self.assertEqual(json.loads(approved.stdout)['entries'][0]['status'], 'confirmed')


if __name__ == '__main__':
    unittest.main()
