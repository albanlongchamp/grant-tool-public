"""Behavioral checks for source-faithful proposal context assembly."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from grantlib import library, nih
from grantlib.retrieval import build_packet


class RetrievalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.sources = self.root / 'sources'
        self.sources.mkdir()
        self.data = self.root / 'data'
        self.db = self.data / 'library.sqlite3'
        self.config = {'sources': [{'name': 'archive', 'path': str(self.sources),
                                   'project_from_subfolder': True, 'default_role': 'ownership_uncertain'}],
                       'data_dir': str(self.data), 'workers': 1}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.sources / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def ingest(self):
        library.ingest(self.config)

    def packet(self, brief=None, **kwargs):
        options = {'max_public': 0, 'max_guidance': 0}
        options.update(kwargs)
        return build_packet(self.db, brief or {'question': 'seedling irrigation'}, **options)

    def test_natural_question_decomposes_and_preserves_exact_evidence(self):
        text = 'We will test irrigation of dormant root tissue. The proposed experiment will compare two storage solutions.'
        self.write('ProjectA/Research_plan.txt', text)
        self.config['vocabulary'] = {'synonyms': {'root': ['root', 'roots'], 'irrigation': ['irrigation', 'irrigated'],
                                                  'dormancy': ['dormant', 'dormancy']}}
        self.ingest()
        packet = self.packet({'question': 'How can we improve the vigor of dormant roots with irrigation?'})
        sources = packet['sources']
        self.assertTrue(sources)
        self.assertIn('root', packet['query_plan']['concepts'])
        self.assertIn('irrigation', packet['query_plan']['concepts'])
        source = next(x for x in sources if x['text'] == text)
        self.assertEqual(source['metadata']['evidence_status'], 'proposed')
        self.assertEqual(source['metadata']['role'], 'ownership_uncertain')
        self.assertEqual(source['metadata']['project'], 'ProjectA')
        self.assertEqual(source['id'], 'P_' + source['metadata']['unit_id'])
        self.assertEqual(source['source_uri'], (self.sources / 'ProjectA/Research_plan.txt').as_uri())

    def test_project_filter_is_strict_even_for_duplicate_text(self):
        text = 'We will measure chlorophyll during seedling irrigation and compare drought injury.'
        self.write('Selected/Research_plan.txt', text)
        self.write('Other/Research_plan.txt', text)
        self.ingest()
        packet = self.packet({'question': 'seedling irrigation', 'projects': ['Selected']})
        self.assertTrue(packet['sources'])
        self.assertEqual({x['metadata']['project'] for x in packet['sources']}, {'Selected'})
        absent = self.packet({'question': 'seedling irrigation', 'projects': ['Missing']})
        self.assertEqual(absent['sources'], [])
        self.assertEqual(absent['coverage']['unknown_projects'], ['Missing'])

    def test_configured_cross_language_aliases_keep_negation_and_uncertainty(self):
        text = 'No hemos demostrado protección de la raíz. Proponemos un estudio de riego en plántulas.'
        self.write('Spanish/Research_plan.txt', text)
        self.config['vocabulary'] = {'synonyms': {'root': ['root', 'raíz'], 'irrigation': ['irrigation', 'riego'],
                                                  'seedling': ['seedling', 'seedlings', 'plántulas']}}
        self.ingest()
        packet = self.packet({'question': 'Root irrigation in seedlings', 'language': 'English'})
        self.assertEqual(packet['sources'][0]['text'], text)
        self.assertNotEqual(packet['sources'][0]['metadata']['evidence_status'], 'completed_result_confirmed')
        self.assertTrue(any('non-English sources' in w for w in packet['warnings']))
        self.write('English/Research_plan.txt', 'We will study root storage and seedling irrigation using a dormant seed model.')
        self.ingest()
        reverse = self.packet({'question': 'riego de la raíz en plántulas', 'projects': ['English']})
        self.assertTrue(reverse['sources'])
        self.assertEqual(reverse['sources'][0]['metadata']['project'], 'English')

    def test_substantive_material_precedes_administration_and_references(self):
        self.write('Project/Research_plan.txt', 'We will study seedling irrigation and root storage with randomized treatment assignment.')
        self.write('Project/Biosketch.txt', 'My seedling irrigation research includes root storage and many papers on seedling irrigation.')
        self.write('Project/Budget.txt', 'Budget for seedling irrigation, root storage, seedling irrigation staff, and seedling irrigation equipment.')
        self.write('Project/References.txt', 'REFERENCES\n\n1. Author. Seedling irrigation and root storage. DOI: 10.9999/example')
        self.ingest()
        packet = self.packet(max_personal=1)
        self.assertEqual(packet['sources'][0]['metadata']['kind'], 'research_strategy')

    def test_diversifies_documents_and_collapses_exact_passages(self):
        for i in range(6):
            text = '\n\n'.join(f'We will study seedling irrigation in experiment {i}, measurement {j}, using a prespecified analysis.' for j in range(5))
            self.write(f'Project{i}/Research_plan.txt', text)
        self.write('Copied/Research_plan.txt', (self.sources / 'Project0/Research_plan.txt').read_text())
        self.ingest()
        packet = self.packet(max_personal=12)
        self.assertEqual(len(packet['sources']), 12)
        counts = {}
        for source in packet['sources']:
            doc = source['metadata']['document_id']
            counts[doc] = counts.get(doc, 0) + 1
        self.assertGreaterEqual(len(counts), 5)
        self.assertLessEqual(max(counts.values()), 3)
        self.assertEqual(len({s['text'] for s in packet['sources']}), len(packet['sources']))

    def test_sources_and_ids_are_stable_and_index_is_read_only(self):
        self.write('Project/Research_plan.txt', 'We will study seedling irrigation and root storage in a controlled experiment.')
        self.ingest()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        with patch('urllib.request.urlopen', side_effect=AssertionError('Network request attempted')):
            first = self.packet()
            second = self.packet()
        self.assertEqual([s['id'] for s in first['sources']], [s['id'] for s in second['sources']])
        self.assertEqual(before, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertFalse((self.data / 'nih.sqlite3').exists())

    def test_hard_budget_marks_exact_prefixes_and_omissions(self):
        for i in range(12):
            self.write(f'Project{i}/Research_plan.txt', f'Study {i}. ' + ('We will measure seedling irrigation, storage and vigor. ' * 50))
        self.ingest()
        packet = self.packet(max_chars=6500)
        self.assertLessEqual(len(json.dumps(packet, ensure_ascii=False, separators=(',', ':'))), 6500)
        self.assertTrue(packet['sources'])
        self.assertTrue(packet['coverage']['sources_omitted_for_budget'] or packet['coverage']['truncated_source_ids'])
        with closing(sqlite3.connect(self.db)) as conn:
            for source in packet['sources']:
                original = conn.execute('SELECT text FROM units WHERE id=?', (source['metadata']['unit_id'],)).fetchone()[0]
                self.assertTrue(original.startswith(source['text']))
                self.assertEqual(source['metadata']['included_text_chars'], len(source['text']))
                self.assertEqual(source['metadata']['truncated'], len(source['text']) < len(original))

    def test_public_records_are_local_grouped_and_not_findings(self):
        self.write('Project/Research_plan.txt', 'We will study seedling irrigation.')
        self.ingest()
        public_path = self.data / 'nih.sqlite3'
        with closing(nih.initialize(public_path)) as conn:
            for app, year, core in [(1234567, 2025, 'R01GM111111'), (1234568, 2026, 'R01GM111111'), (1234569, 2026, 'R01GM222222')]:
                nih.import_api_record(conn, {'appl_id': app, 'fiscal_year': year, 'core_project_num': core,
                    'project_num': core, 'project_title': 'Seedling irrigation mechanisms',
                    'abstract_text': 'We propose to study seedling irrigation and seed storage.', 'activity_code': 'R01'})
            conn.commit()
        before = hashlib.sha256(public_path.read_bytes()).hexdigest()
        packet = self.packet(max_public=8)
        public = [s for s in packet['sources'] if s['layer'] == 'public_project']
        self.assertEqual(len(public), 2)
        self.assertIn('N_1234568', {s['id'] for s in public})
        self.assertNotIn('N_1234567', {s['id'] for s in public})
        self.assertTrue(all(s['metadata']['evidence_status'] == 'funded_project_description' for s in public))
        self.assertEqual(before, hashlib.sha256(public_path.read_bytes()).hexdigest())

    def test_guidance_scopes_and_effective_dates_remain_explicit(self):
        self.write('Project/Research_plan.txt', 'We will study seedling irrigation.')
        self.ingest()
        packet = self.packet({'question': 'seedling irrigation', 'mechanism': 'K23', 'due_date': '2026-10-30'},
                             max_guidance=30, max_chars=100000)
        ids = {s['id'] for s in packet['sources'] if s['layer'] == 'guidance'}
        self.assertNotIn('G_nih-simplified-research-review', ids)
        self.assertNotIn('G_nih-career-program-updates-2027', ids)
        self.assertIn('nih-career-program-updates-2027', packet['coverage']['guidance']['future_policy_ids_omitted'])
        for source in packet['sources']:
            if source['layer'] == 'guidance':
                self.assertIn('checked_on', source['metadata'])
                self.assertEqual(source['metadata']['timing_basis'], 'proposal_due_date')
                self.assertFalse(source['metadata']['quoted_verbatim'])
        unsupported = self.packet({'question': 'seedling', 'mechanism': 'UNRECOGNIZED'}, max_guidance=8)
        self.assertFalse(any(s['layer'] == 'guidance' for s in unsupported['sources']))

    def test_unknown_terms_do_not_silently_browse_whole_archive(self):
        self.write('Project/Research_plan.txt', 'We will study seedling irrigation.')
        self.ingest()
        self.assertEqual(self.packet({'question': 'zyxxyunknown'})['sources'], [])
        scoped = self.packet({'question': 'New proposal', 'projects': ['Project']})
        self.assertTrue(scoped['sources'])
        self.assertTrue(all(s['metadata']['project'] == 'Project' for s in scoped['sources']))

    def test_invalid_input_is_rejected(self):
        self.write('Project/Research_plan.txt', 'We will study seedling irrigation.')
        self.ingest()
        for brief, options in [({'projects': 'Project'}, {}), ({'keywords': [None]}, {}),
                               ({'due_date': '2026-99-99', 'mechanism': 'R01'}, {'max_guidance': 1}),
                               ({}, {'max_chars': 50}), ({}, {'max_personal': -1})]:
            with self.subTest(brief=brief, options=options), self.assertRaises(ValueError):
                self.packet(brief, **options)


if __name__ == '__main__':
    unittest.main()
