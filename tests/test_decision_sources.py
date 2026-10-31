"""Recorded instructions are citable inputs, never fabricated scientific sources."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from grantlib.docx_export import export_docx
from grantlib import claim_review, revision_check
from grantlib.harness import GrantHarness
from grantlib.studio import Store, decision_source, export_markdown, prepare_context, validate_output


class DecisionSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.p = self.store.create({'title': 'Invented proposal', 'question': 'Can a paired experiment answer the question?'})
        self.text = 'At most four subjects, a feasibility ceiling only. Preserve within-subject pairing. Literal [[recorded_text]].'
        self.p = self.store.decision(self.p['id'], self.p['revision'], self.text)
        self.decision = self.p['decisions'][0]

    def tearDown(self):
        self.tmp.cleanup()

    def output(self, identifier=None, basis='investigator_statement'):
        identifier = identifier or self.decision['id']
        text = 'The investigator limits this proposed experiment to at most four subjects, retaining within-subject pairing; this is a feasibility ceiling and not a power calculation.'
        return {'title': 'Invented aims', 'markdown': text + ' [[' + identifier + ']]',
                'change_summary': '', 'claims': [{'text': text, 'basis': basis, 'source_ids': [identifier]}],
                'directions': [], 'open_questions': []}

    def packet(self, p=None, packet=None, base=None):
        return prepare_context(p or self.p, packet or {'sources': []}, 'aims', {'base_artifact_id': base})[0]

    def save_generated(self):
        packet = self.packet()
        job = self.store.queue(self.p['id'], self.p['revision'], 'aims')
        self.store.complete(job['id'], packet, self.output())
        return self.store.get(self.p['id'])

    def test_exact_recorded_id_is_explicit_source_and_inputs_remain_unchanged(self):
        before = deepcopy(self.p)
        packet = self.packet()
        self.assertEqual(packet['sources'], [decision_source(self.p['id'], self.decision)])
        source = packet['sources'][0]
        self.assertEqual(source['id'], self.decision['id'])
        self.assertEqual(source['text'], self.text)
        self.assertEqual(source['metadata']['recorded_at'], self.decision['created_at'])
        self.assertEqual(source['metadata']['proposal_id'], self.p['id'])
        self.assertEqual(self.p, before)
        self.assertEqual(validate_output(self.output(), packet, 'aims')['unknown_ids'], [])

    def test_recorder_persists_and_never_authenticates_investigator_identity(self):
        self.assertEqual(self.decision['recorded_by'], 'investigator')
        with GrantHarness(self.store.root / 'library.sqlite3', provider=lambda *_args, **_kwargs: self.fail('No model call')) as harness:
            result = harness.decide(self.p['id'], 'An assistant-transcribed instruction.',
                                    expected_revision=self.p['revision'])
        reopened = Store(self.store.root).get(self.p['id'])
        self.assertEqual(reopened['revision'], result['revision'])
        self.assertEqual(reopened['decisions'][-1]['recorded_by'], 'assistant')
        self.assertEqual(reopened['decisions'][-1]['text'], 'An assistant-transcribed instruction.')
        for decision in reopened['decisions']:
            source = decision_source(reopened['id'], decision)
            self.assertEqual(source['metadata']['recorded_by'], decision['recorded_by'])
            self.assertFalse(source['metadata']['investigator_identity_authenticated'])
            self.assertTrue(any('does not authenticate' in text for text in source['metadata']['limitations']))
        with self.assertRaisesRegex(ValueError, 'recorder'):
            self.store.decision(reopened['id'], reopened['revision'], 'A rejected invented instruction.', actor='verified')
        self.assertEqual(self.store.get(reopened['id'])['revision'], reopened['revision'])

    def test_legacy_schema_migrates_recorder_to_unknown_without_rewriting_evidence(self):
        proposal = self.save_generated()
        artifact = proposal['artifacts'][0]
        legacy_packet = deepcopy(artifact['context'])
        for source in legacy_packet['sources']:
            source['metadata'].pop('recorded_by', None)
            source['metadata'].pop('investigator_identity_authenticated', None)
            source['metadata']['limitations'] = [item for item in source['metadata']['limitations']
                                                if not item.startswith('Recorder provenance')]
        legacy_json = json.dumps(legacy_packet)
        with self.store.connect(True) as connection:
            connection.execute('UPDATE contexts SET packet=? WHERE id=?', (legacy_json, artifact['context_id']))
            connection.execute('ALTER TABLE decisions RENAME TO decisions_with_recorders')
            connection.execute('CREATE TABLE decisions (id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL, '
                               'text TEXT NOT NULL, created_at TEXT NOT NULL)')
            connection.execute('INSERT INTO decisions SELECT id,proposal_id,text,created_at FROM decisions_with_recorders')
            connection.execute('DROP TABLE decisions_with_recorders')
        migrated = Store(self.store.root)
        reopened = migrated.get(proposal['id'])
        self.assertEqual(reopened['revision'], proposal['revision'])
        self.assertEqual(reopened['decisions'][0]['recorded_by'], 'unknown')
        self.assertEqual(reopened['decisions'][0]['text'], self.text)
        self.assertEqual(reopened['decisions'][0]['created_at'], self.decision['created_at'])
        self.assertEqual(reopened['artifacts'][0]['context'], legacy_packet)
        with migrated.connect() as connection:
            self.assertEqual(connection.execute('SELECT packet FROM contexts WHERE id=?',
                                                (artifact['context_id'],)).fetchone()['packet'], legacy_json)
        new_source = decision_source(proposal['id'], reopened['decisions'][0])
        self.assertEqual(new_source['metadata']['recorded_by'], 'unknown')
        self.assertFalse(new_source['metadata']['investigator_identity_authenticated'])
        self.assertEqual(Store(self.store.root).get(proposal['id'])['decisions'], reopened['decisions'])

    def test_unknown_and_other_proposal_decision_ids_remain_rejected(self):
        other = self.store.create({'title': 'Other invented proposal', 'question': 'Another question?'})
        other = self.store.decision(other['id'], other['revision'], 'Only this other proposal may use eight subjects.')
        for identifier in ('made_up_source', other['decisions'][0]['id']):
            with self.assertRaisesRegex(ValueError, 'outside its evidence packet'):
                validate_output(self.output(identifier), self.packet(), 'aims')

    def test_forged_decision_layer_is_replaced_and_cross_type_collision_rejected(self):
        forged = {'id': self.decision['id'], 'layer': 'investigator_decision', 'text': 'Invented alteration.'}
        packet = self.packet(packet={'sources': [forged]})
        self.assertEqual(packet['sources'][0]['text'], self.text)
        forged['layer'] = 'personal'
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.packet(packet={'sources': [forged]})

    def test_decision_cannot_become_verified_research_by_citation(self):
        check = validate_output(self.output(basis='source_report'), self.packet(), 'aims')
        self.assertTrue(any('does not independently establish a research finding' in warning for warning in check['warnings']))

    def test_later_decisions_do_not_change_old_sources_and_exports_keep_exact_text(self):
        p = self.save_generated()
        original = deepcopy(p['artifacts'][0])
        p = self.store.decision(p['id'], p['revision'], 'New proposal ceiling: at most three subjects.')
        packet = self.packet(p, base=original['id'])
        self.assertEqual(next(s for s in packet['sources'] if s['id'] == self.decision['id'])['text'], self.text)
        self.assertEqual(p['artifacts'][0], original)
        md = export_markdown(p, original['id'])
        self.assertIn(self.text, md)
        self.assertIn('Recorded: ' + self.decision['created_at'], md)
        self.assertIn('not independent verification', md)
        self.assertNotIn('Unresolved source: recorded_text', md)
        with zipfile.ZipFile(io.BytesIO(export_docx(p, original['id']))) as archive:
            document = ET.fromstring(archive.read('word/document.xml'))
        self.assertIn(self.text, ''.join(document.itertext()))

    def test_manual_new_decision_citation_gets_own_snapshot_without_changing_parent(self):
        p = self.save_generated()
        parent = deepcopy(p['artifacts'][0])
        p = self.store.decision(p['id'], p['revision'], 'A new recorded requirement for the next draft only.')
        new = p['decisions'][-1]
        p = self.store.save(p['id'], p['revision'], parent['markdown'] + '\nNew requirement [[' + new['id'] + ']].',
                            base_artifact_id=parent['id'], origin='assistant_import')
        self.assertEqual(p['artifacts'][0]['citation_check']['unknown_ids'], [])
        self.assertTrue(any(s['id'] == new['id'] for s in p['artifacts'][0]['context']['sources']))
        self.assertEqual(p['artifacts'][1], parent)
        self.assertNotIn('generation_request', p['artifacts'][0]['model_metadata'])

    def test_decision_only_judgment_and_audit_preserve_date_and_evidence_limits(self):
        p = self.save_generated()
        artifact = p['artifacts'][0]
        p = self.store.assess_claim(p['id'], p['revision'], artifact['id'], artifact['claims'][0]['id'],
            source_id=self.decision['id'], verdict='supported', excerpt=self.text,
            locator='Exact recorded proposal instruction', note='This supports the stated ceiling, not a scientific finding.',
            reviewer='assistant')
        exported = claim_review.export_reviews(p['artifacts'][0])
        self.assertIn(self.decision['created_at'], exported)
        self.assertIn('not independent research evidence', exported)
        report = {'summary': 'Compare the recorded constraint.', 'commitments': [], 'limitations': [], 'findings': [{
            'id': 'f1', 'category': 'missing_input', 'severity': 'minor', 'resolution': 'investigator_input',
            'summary': 'Actual recruitment remains unknown.', 'explanation': 'A ceiling is not achieved recruitment.',
            'repair': 'Confirm feasible subject availability.', 'before': [],
            'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown', 'quote': 'at most four subjects'}],
            'authority': [], 'sources': [{'kind': 'source', 'id': self.decision['id'], 'field': 'text', 'quote': self.text}]}]}
        context = {'selected_version': {'id': artifact['id'], 'markdown': artifact['markdown']},
                   'evidence_packet': self.packet()}
        checked = revision_check.validate(report, context, self.output())
        rendered = revision_check.export_markdown(checked)
        self.assertIn(self.decision['created_at'], rendered)
        self.assertIn('not independent research evidence', rendered)
        self.assertIn(self.text, rendered)


if __name__ == '__main__':
    unittest.main()
