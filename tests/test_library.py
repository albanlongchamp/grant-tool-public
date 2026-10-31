import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grantlib.library import (
    _prepare_units, curate_document, curate_unit, get_document, ingest, list_documents,
    load_config, search, stats,
)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.data = self.root / 'data'
        self.db = self.data / 'library.sqlite3'
        self.config = {'sources': [{'name': 'test', 'path': str(self.source)}],
                       'data_dir': str(self.data), 'workers': 1}

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, text):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_coverage_includes_unsupported_and_locks(self):
        self.write('methods.txt', 'We will measure chlorophyll in leaf tissue.')
        self.write('image.png', 'not interpreted')
        self.write('~$draft.docx', 'lock')
        report = ingest(self.config)
        self.assertEqual(report['counts']['documents'], 3)
        self.assertEqual(report['counts']['asset_only'], 1)
        self.assertEqual(report['counts']['ignored'], 1)
        self.assertEqual(search(self.db, 'chlorophyll')[0]['evidence_status'], 'proposed')

    def test_duplicate_occurrences_remain_filterable_and_curated(self):
        text = 'We will measure chlorophyll in leaf tissue.'
        self.write('previous_submission/strategy.txt', text)
        ingest(self.config)
        hit = search(self.db, 'chlorophyll')[0]
        curate_unit(self.db, hit['id'], 'proposed', 'Checked original aim; work remains proposed.')
        self.write('resubmission/strategy.txt', text)
        self.write('root.txt', text)
        ingest(self.config)
        old = search(self.db, 'chlorophyll', {'cycle': 'historical'})
        self.assertEqual(len(old), 1)
        self.assertEqual(old[0]['id'], hit['id'])
        self.assertIn('Checked original', old[0]['review_note'])
        docs = list_documents(self.db)
        primary = [d for d in docs if not d['duplicate_of']]
        self.assertEqual(primary[0]['cycle'], 'resubmission')
        duplicate = next(d for d in docs if d['cycle'] == 'historical')
        self.assertEqual(get_document(self.db, duplicate['id'])['units'][0]['document_id'], duplicate['id'])
        self.assertEqual(len(search(self.db, 'chlorophyll')), 1)

    def test_collaborator_excluded_from_default_search(self):
        self.write('collaborators/example.txt', 'We have demonstrated seedling protection with auxin.')
        self.write('my_methods.txt', 'We will measure chlorophyll in leaf tissue.')
        ingest(self.config)
        self.assertEqual(search(self.db, 'auxin'), [])
        self.assertEqual(search(self.db, 'auxin', {'role': 'all'})[0]['role'], 'collaborator_or_template')

    def test_transient_extractor_error_is_retried(self):
        self.write('methods.txt', 'We will measure chlorophyll.')
        error = {'units': [], 'warnings': ['temporary failure'], 'metadata': {'status': 'error'}}
        with patch('grantlib.library.extract_file', return_value=error):
            self.assertEqual(ingest(self.config)['counts']['error'], 1)
        ingest(self.config)
        self.assertEqual(len(search(self.db, 'chlorophyll')), 1)

    def test_missing_source_retains_previous_snapshot(self):
        self.write('methods.txt', 'We will measure chlorophyll.')
        ingest(self.config)
        before = self.db.read_bytes()
        self.config['sources'][0]['path'] = str(self.root / 'missing')
        with self.assertRaises(ValueError):
            ingest(self.config)
        self.assertEqual(self.db.read_bytes(), before)

    def test_symlinks_are_not_followed(self):
        outside = self.root / 'outside.txt'
        outside.write_text('Private unrelated auxin text')
        (self.source / 'link.txt').symlink_to(outside)
        result = ingest(self.config)
        self.assertEqual(result['counts']['ignored'], 1)
        self.assertEqual(search(self.db, 'auxin'), [])

    def test_long_excerpts_preserve_text_and_source_offsets(self):
        text = 'METHODS\n' + ('We will measure chlorophyll in leaf tissue. ' * 120)
        units = list(_prepare_units([{'text': text, 'locator': 'page 3, block 2', 'kind': 'paragraph'}]))
        self.assertGreater(len(units), 1)
        self.assertEqual(''.join(x['text'] for x in units), text.strip())
        self.assertTrue(all(x['locator'].startswith('page 3, block 2; excerpt chars') for x in units))
        self.assertEqual(units[0]['section'], 'Methods')

    def test_fts_input_is_not_an_expression(self):
        self.write('methods.txt', 'We will measure chlorophyll in leaf tissue.')
        ingest(self.config)
        self.assertIsInstance(search(self.db, 'chlorophyll " OR * NEAR('), list)
        self.assertEqual(search(self.db, 'auxin', {'category': "x') OR 1=1 --"}), [])

    def test_vocabulary_configuration_is_validated(self):
        path = self.root / 'config.json'
        for vocabulary in ({'topics': {'Bad Tag': 'x'}}, {'topics': {'ok': '('}},
                           {'synonyms': {'ok': []}}, {'unknown': {}}):
            path.write_text(json.dumps(dict(self.config, vocabulary=vocabulary)))
            with self.assertRaises(ValueError):
                load_config(path)
        path.write_text(json.dumps(dict(self.config, vocabulary={'topics': {'soil': r'\bsoil\b'}})))
        self.assertEqual(load_config(path)['vocabulary'], {'topics': {'soil': r'\bsoil\b'}, 'synonyms': {}})

    def test_configured_topics_annotate_units(self):
        self.write('methods.txt', 'We will sample soil cores.')
        ingest(dict(self.config, vocabulary={'topics': {'soil': r'\bsoil\b'}}))
        self.assertEqual(search(self.db, 'cores', {'topic': 'soil'})[0]['topics'], ['soil'])

    def test_multiword_terms_match_across_sentence(self):
        self.write('methods.txt', 'We will measure chlorophyll in leaf tissue during irrigation.')
        ingest(dict(self.config, vocabulary={'synonyms': {'leaf': ['leaf', 'foliar']}}))
        self.assertEqual(len(search(self.db, 'leaf irrigation')), 1)
        self.assertEqual(len(search(self.db, 'foliar irrigation')), 1)
        self.assertEqual(len(search(self.db, 'root irrigation')), 0)

    def test_pdf_section_does_not_leak_across_attachments(self):
        units = list(_prepare_units([
            {'text': 'REFERENCES', 'locator': 'page 1', 'page': 1, 'kind': 'paragraph'},
            {'text': 'We found a result in our study.', 'locator': 'page 2', 'page': 2, 'kind': 'paragraph'},
        ]))
        self.assertFalse(units[1].get('section'))

    def test_phrase_synonym_retrieves_named_technique(self):
        self.write('methods.txt', 'We will use Visium to assess gene expression.')
        ingest(self.config)
        self.assertEqual(len(search(self.db, 'spatial transcriptomics')), 1)

    def test_exclusions_prune_subtrees_and_exact_files_before_reading(self):
        self.write('methods.txt', 'We will measure chlorophyll in leaf tissue.')
        self.write('generated/inner/summary.txt', 'Generated auxin summary.')
        self.write('generated_elsewhere/strategy.txt', 'We will assess irrigation.')
        self.write('Project/AGENTS.md', 'Instructions are not scientific evidence.')
        self.write('Project/aims.txt', 'We will test auxin.')
        exclusions = ['generated', 'Project/AGENTS.md']
        self.config['sources'][0]['exclude_paths'] = exclusions
        visited = []
        real_walk = os.walk
        def observed_walk(*args, **kwargs):
            for entry in real_walk(*args, **kwargs):
                visited.append(Path(entry[0]))
                yield entry
        with patch('grantlib.library.os.walk', side_effect=observed_walk):
            report = ingest(self.config)
        self.assertEqual(report['counts']['documents'], 3)
        self.assertNotIn(self.source / 'generated', visited)
        self.assertEqual({d['relative_path'] for d in list_documents(self.db)},
                         {'methods.txt', 'generated_elsewhere/strategy.txt', 'Project/aims.txt'})
        self.assertEqual(report['sources'][0]['exclude_paths'], exclusions)
        self.assertEqual((self.source / 'generated/inner/summary.txt').read_text(), 'Generated auxin summary.')

    def test_source_controls_reject_invalid_configuration(self):
        invalid = [
            ('exclude_paths', 'generated'), ('exclude_paths', ['']),
            ('exclude_paths', [' ']), ('exclude_paths', ['.']),
            ('exclude_paths', ['/absolute']), ('exclude_paths', ['../outside']),
            ('exclude_paths', ['folder/../outside']), ('exclude_paths', ['./folder']),
            ('exclude_paths', ['C:\\outside']), ('exclude_paths', [None]),
            ('default_role', 'guaranteed_owner'), ('default_role', []),
            ('project_from_subfolder', 'true'),
        ]
        config_path = self.root / 'config.json'
        for key, value in invalid:
            with self.subTest(key=key, value=value):
                config = json.loads(json.dumps(self.config))
                config['sources'][0][key] = value
                config_path.write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    load_config(config_path)
                with self.assertRaises(ValueError):
                    ingest(config)
        self.assertFalse(self.data.exists())

    def test_adding_parent_source_preserves_child_identity_and_review(self):
        self.write('K23/strategy.txt', 'We will measure chlorophyll in leaf tissue.')
        self.config['sources'][0] = {'name': 'K23', 'path': str(self.source / 'K23')}
        ingest(self.config)
        before_document = list_documents(self.db)[0]
        before_unit = search(self.db, 'chlorophyll')[0]
        curate_unit(self.db, before_unit['id'], 'proposed', 'Checked original K23 aim.')
        self.write('R01/strategy.txt', 'We will assess irrigation in seed lots.')
        self.write('loose.txt', 'We will test a chloroplast intervention.')
        self.write('generated_notes/summary.md', 'Generated duplicate summary.')
        self.config['sources'].append({
            'name': 'Grant', 'path': str(self.source),
            'exclude_paths': ['K23', 'generated_notes'],
            'default_role': 'ownership_uncertain', 'project_from_subfolder': True,
        })
        report = ingest(self.config)
        after_document = get_document(self.db, before_document['id'])
        after_unit = search(self.db, 'chlorophyll')[0]
        self.assertEqual(report['counts']['documents'], 3)
        self.assertEqual(after_document['source_path'], before_document['source_path'])
        self.assertEqual(after_unit['id'], before_unit['id'])
        self.assertEqual(after_unit['review_note'], 'Checked original K23 aim.')
        self.assertEqual(after_unit['project'], 'K23')
        self.assertEqual({d['project'] for d in list_documents(self.db)}, {'K23', 'R01', 'Grant'})

    def test_configured_default_role_preserves_explicit_classification_and_curation(self):
        self.write('Project/strategy.txt', 'We will measure chlorophyll.')
        self.write('collaborators/strategy.txt', 'We will measure seedling irrigation.')
        self.write('literature/article.txt', 'Published background on leaf blight.')
        self.write('unreviewed_archive/strategy.txt', 'We will test auxin.')
        self.config['sources'][0]['default_role'] = 'ownership_uncertain'
        ingest(self.config)
        documents = {d['relative_path']: d for d in list_documents(self.db)}
        fallback = documents['Project/strategy.txt']
        self.assertEqual(fallback['role'], 'ownership_uncertain')
        self.assertIn('document_role_from_source_default', fallback['review_flags'])
        self.assertEqual(documents['collaborators/strategy.txt']['role'], 'collaborator_or_template')
        self.assertEqual(documents['literature/article.txt']['role'], 'literature')
        self.assertEqual(documents['unreviewed_archive/strategy.txt']['role'], 'ownership_uncertain')
        self.assertNotIn('document_role_from_source_default', documents['unreviewed_archive/strategy.txt']['review_flags'])
        self.assertEqual(search(self.db, 'chlorophyll'), [])
        self.assertEqual(len(search(self.db, 'chlorophyll', {'role': 'all'})), 1)
        curate_document(self.db, fallback['id'], 'own_grant')
        ingest(self.config)
        self.assertEqual(search(self.db, 'chlorophyll')[0]['role'], 'own_grant')

    def test_projects_group_folders_and_filter_duplicate_occurrences(self):
        text = 'We will measure chlorophyll in leaf tissue.'
        self.write('ProjectA/strategy.txt', text)
        self.write('ProjectB/revision/strategy.txt', text)
        self.write('loose.txt', 'We will assess irrigation.')
        self.config['sources'][0]['project_from_subfolder'] = True
        report = ingest(self.config)
        self.assertEqual({x['value']: x['count'] for x in report['facets']['project']},
                         {'ProjectA': 1, 'ProjectB': 1, 'test': 1})
        for project in ('ProjectA', 'ProjectB'):
            hits = search(self.db, 'chlorophyll', {'project': project})
            self.assertEqual(len(hits), 1)
            self.assertEqual(hits[0]['project'], project)
            self.assertEqual(list_documents(self.db, {'project': project})[0]['project'], project)
        self.assertEqual(len(search(self.db, 'chlorophyll')), 1)
        self.assertEqual(search(self.db, 'chlorophyll', {'project': "x' OR 1=1 --"}), [])


if __name__ == '__main__':
    unittest.main()
