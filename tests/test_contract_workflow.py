"""Injected Studio workflows exercise explicit experimental audit selection.

No inference, archive, live resource or scientific-quality claim is involved.
"""
from contextlib import redirect_stdout
from copy import deepcopy
from io import BytesIO, StringIO
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from grantlib import audit_contract, audit_context, draft_check
from grantlib.docx_export import export_docx
from grantlib.studio import Service, Store, export_markdown
from scripts import evaluate_assistant as harness


VERSION = 'scientific_defects_v1'
OBSERVATION = 'The fictional paired-subject comparison remains proposed.'
SUBSTANTIVE = OBSERVATION + ' Subjects remain independent units, and unresolved methods require investigator input.'


def digest(value, *, compact=False):
    options = {'separators': (',', ':')} if compact else {}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False, **options).encode()).hexdigest()


class WorkflowGenerator:
    """Respond according to the actual received schema, retaining exact wires."""

    def __init__(self, *, repair=False, invalid_stage=None, persistent=False):
        self.repair = repair
        self.invalid_stage = invalid_stage
        self.persistent = persistent
        self.calls = []

    def __call__(self, prompt, **options):
        stage = Path(options['run_dir']).name
        payload = harness.prompt_data(prompt)
        context = payload.get('context', payload)
        audit = 'findings' in options['schema']['properties']
        revision_audit = audit and 'commitments' in options['schema']['properties']
        if audit and not revision_audit:
            action = payload['action']
        elif revision_audit or context['investigator_decisions']:
            action = 'revise'
        elif context['selected_version'] is None:
            action = 'design'
        elif context['selected_version']['kind'] == 'design':
            action = 'aims'
        else:
            action = 'review'
        stage = stage if action != 'review' else 'generate'
        if audit:
            output = {'summary': 'Injected comparison, not scientific verification.',
                      'findings': [], 'limitations': ['Only pipeline behavior is tested.']}
            if revision_audit:
                output['commitments'] = []
            if self.repair and (stage == 'check-initial' or self.persistent):
                source = next(s for s in context['evidence_packet']['sources']
                              if s['id'] == ('P_resources_v2' if action == 'revise' else 'P_resources_v1'))
                output['findings'] = [{
                    'id': 'injected_scope', 'category': 'interpretation',
                    'severity': 'major', 'resolution': 'draft_repair',
                    'summary': 'Injected resource-scope concern.',
                    'explanation': 'This accepted synthetic finding exercises bounded routing.',
                    'repair': 'Retain the current planning scope and qualify the draft.',
                    'before': ([{'kind': 'parent', 'id': context['selected_version']['id'],
                                'field': 'markdown', 'quote': OBSERVATION}] if revision_audit else []),
                    'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown', 'quote': OBSERVATION}],
                    'authority': [],
                    'sources': [{'kind': 'source', 'id': source['id'], 'field': 'text',
                                 'quote': source['text']}],
                }]
            if 'contract_version' in options['schema']['properties']:
                output['contract_version'] = VERSION
                if stage == self.invalid_stage:
                    output['contract_version'] = 'unrecognized_contract'
        else:
            source_id = 'P_resources_v2' if action == 'revise' else 'P_resources_v1'
            output = {
                'title': 'Fictional workflow draft',
                'markdown': OBSERVATION + (' The scope remains conditional after repair.' if stage == 'repair'
                                           else ' Resource and precision choices remain investigator inputs.')
                            + ' [[' + source_id + ']]',
                'change_summary': 'An injected intermediate repair.' if stage == 'repair' else '',
                'claims': [{'text': 'The resource statement is an attributed planning constraint.',
                            'basis': 'investigator_statement', 'source_ids': [source_id]}],
                'directions': ([{'id': 'D1', 'title': 'Paired direction', 'description': 'An invented comparison.'},
                                {'id': 'D2', 'title': 'Alternative', 'description': 'Another invented comparison.'}]
                               if action == 'design' else []),
                'open_questions': ['Which precision and feasibility assumptions remain to be established?'],
            }
        self.calls.append({'action': action, 'stage': stage, 'prompt': prompt,
                           'schema': deepcopy(options['schema']), 'payload': deepcopy(payload),
                           'wire': deepcopy(output)})
        return {'status': 'completed', 'structured': output, 'provider': 'injected',
                'model': 'fictional', 'run_id': 'injected-' + str(len(self.calls)),
                'usage': {'input_tokens': 1, 'output_tokens': 2}}


class ContractWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_workflow(self, *, contract=VERSION, **options):
        directory = harness.prepare(self.root / ('run-' + str(len(list(self.root.iterdir())))),
                                    audit_contract=contract)
        generate = WorkflowGenerator(**options)
        original_connect = sqlite3.connect

        def isolated_connect(path, *args, **kwargs):
            self.assertEqual(Path(path).resolve(), directory / 'studio.sqlite3')
            return original_connect(path, *args, **kwargs)

        with patch('sqlite3.connect', isolated_connect), redirect_stdout(StringIO()):
            summary = harness.run_evaluation(directory, generate=generate, timeout=5,
                                             audit_contract=contract)
        proposal = json.loads((directory / 'proposal-final.json').read_text())
        self.assertFalse((directory / 'synthetic-context-only.sqlite3').exists())
        self.assertEqual(Store(directory).memory_catalog()['entries'], [])
        return directory, generate, summary, proposal

    def test_explicit_contract_reaches_all_audits_but_review_stays_one_generation(self):
        directory, generator, summary, proposal = self.run_workflow()
        self.assertEqual(summary['audit_contract'], VERSION)
        self.assertEqual(summary['model_calls'], 7)
        self.assertTrue(summary['call_plan_satisfied'])
        self.assertTrue(summary['earlier_artifacts_unchanged'])
        self.assertTrue(summary['investigator_decisions_unchanged'])
        self.assertTrue(summary['investigator_brief_unchanged'])
        for call in generator.calls:
            if call['stage'].startswith('check-'):
                prefix = 'REVISION' if call['action'] == 'revise' else 'DRAFT'
                self.assertTrue(call['prompt'].startswith(prefix + '_SCIENTIFIC_DEFECTS_V1_QUOTES\n'))
                self.assertIn('contract_version', call['schema']['required'])
                self.assertNotIn('authorized_change',
                    call['schema']['properties']['findings']['items']['properties']['category']['enum'])
                self.assertNotIn('passage_registry', call['payload'])
            else:
                self.assertNotIn('contract_version', call['schema']['properties'])
        review_calls = [c for c in generator.calls if c['action'] == 'review']
        self.assertEqual(len(review_calls), 1)
        self.assertNotIn('previous_draft_check', json.dumps(review_calls[0]['payload']))
        review = next(a for a in proposal['artifacts'] if a['kind'] == 'review')
        self.assertNotIn('draft_check', review['model_metadata'])
        self.assertNotIn('revision_check', review['model_metadata'])
        self.assertEqual([c['protocol'] for c in summary['calls'] if c['stage'].startswith('check-')],
                         ['draft_scientific_defects_v1_quotes', 'draft_scientific_defects_v1_quotes',
                          'revision_scientific_defects_v1_quotes'])

    def test_one_repair_per_job_preserves_exact_wire_context_and_exported_science(self):
        directory, generator, summary, proposal = self.run_workflow(repair=True, persistent=True)
        self.assertEqual(summary['model_calls'], 13)
        self.assertEqual(sum(c['stage'] == 'repair' for c in generator.calls), 3)
        self.assertTrue(summary['call_plan_satisfied'])
        for job in summary['jobs']:
            if job['action'] == 'review':
                continue
            action = job['action']
            artifact = next(a for a in proposal['artifacts'] if a['id'] == job['artifact_id'])
            calls = {c['stage']: c for c in generator.calls if c['action'] == action}
            check = artifact['model_metadata']['revision_check' if action == 'revise' else 'draft_check']
            self.assertTrue(check['repaired'])
            self.assertEqual(check['status'], 'completed')
            self.assertTrue(check['report']['repair_needed'])  # A second repair is deliberately not run.
            self.assertEqual(artifact['structured'], calls['repair']['wire'])
            self.assertEqual(check['candidate_sha256'], digest(artifact['structured']))
            self.assertEqual(check['context_sha256'], digest(calls['draft']['payload']))
            expected_context, _ = audit_context.prepare(calls['draft']['payload'])
            for stage in ('check-initial', 'check-final'):
                call = calls[stage]
                self.assertEqual(call['payload']['context'], expected_context)
                step = next(s for s in check['steps'] if s['stage'] == stage)
                report = step['report']
                self.assertEqual(report['contract_version'], VERSION)
                self.assertEqual(report['validation']['raw_report_sha256'], digest(call['wire']))
                self.assertEqual(report['audit_contract'], {
                    'anchor_format': 'quotes', 'input_sha256': digest(call['payload'], compact=True),
                    'hash_scope': 'canonical_audit_input_json'})
                self.assertEqual(report['findings'][0]['sources'][0]['quote'],
                                 call['wire']['findings'][0]['sources'][0]['quote'])
            self.assertEqual(calls['check-final']['payload']['candidate'], calls['repair']['wire'])
            self.assertNotIn('injected_scope', json.dumps(calls['check-final']['payload']))
            if action != 'revise':
                baseline = draft_check.baseline_snapshot(calls['draft']['wire'])
                self.assertEqual(calls['check-final']['payload']['repair_baseline'], baseline)
                self.assertEqual(check['repair_baseline'], baseline)
            text = export_markdown(proposal, artifact['id'])
            self.assertIn('not investigator approval', text)
            self.assertIn('Injected resource-scope concern.', text)
            source = check['report']['findings'][0]['sources'][0]
            self.assertIn(source['quote'], text)
            self.assertIn(source['provenance']['metadata']['evidence_status'], text)
            if action != 'revise':
                self.assertIn(calls['draft']['wire']['markdown'], text)
                self.assertIn(check['repair_baseline']['candidate_sha256'], text)
            if action == 'revise':
                with zipfile.ZipFile(BytesIO(export_docx(proposal, artifact['id']))) as archive:
                    xml = archive.read('word/document.xml').decode()
                self.assertIn('Injected resource-scope concern.', xml)
                self.assertIn('Automated revision check', xml)
        self.assertTrue(summary['earlier_artifacts_unchanged'])

    def test_invalid_initial_contract_is_incomplete_without_legacy_fallback_or_extra_call(self):
        _, generator, summary, proposal = self.run_workflow(invalid_stage='check-initial')
        self.assertEqual(summary['model_calls'], 7)
        self.assertNotIn('repair', [c['stage'] for c in generator.calls])
        for job in summary['jobs']:
            if job['action'] == 'review':
                continue
            artifact = next(a for a in proposal['artifacts'] if a['id'] == job['artifact_id'])
            check = artifact['model_metadata']['revision_check' if job['action'] == 'revise' else 'draft_check']
            self.assertEqual(check['status'], 'incomplete')
            self.assertIsNone(check['report'])
            self.assertEqual(check['failed_stages'], ['check-initial'])
            self.assertIn('incomplete', export_markdown(proposal, artifact['id']))
            self.assertFalse(check['repaired'])

    def test_invalid_final_contract_retains_repaired_version_without_stale_verdict(self):
        _, generator, summary, proposal = self.run_workflow(repair=True, invalid_stage='check-final')
        self.assertEqual(summary['model_calls'], 13)
        for job in summary['jobs']:
            if job['action'] == 'review':
                continue
            artifact = next(a for a in proposal['artifacts'] if a['id'] == job['artifact_id'])
            check = artifact['model_metadata']['revision_check' if job['action'] == 'revise' else 'draft_check']
            self.assertTrue(check['repaired'])
            self.assertIsNone(check['report'])
            self.assertEqual(check['status'], 'incomplete')
            self.assertEqual(check['failed_stages'], ['check-final'])
            repair = next(c['wire'] for c in generator.calls
                          if c['action'] == job['action'] and c['stage'] == 'repair')
            self.assertEqual(artifact['structured'], repair)
            exported = export_markdown(proposal, artifact['id'])
            self.assertNotIn('Injected resource-scope concern.', exported)
            self.assertIn('incomplete', exported)

    def test_default_remains_legacy_and_preparation_prevents_contract_switching(self):
        _, generator, summary, _ = self.run_workflow(contract='legacy')
        self.assertEqual(summary['model_calls'], 7)
        for call in generator.calls:
            self.assertNotIn('contract_version', call['schema']['properties'])
        directory = harness.prepare(self.root / 'new-contract', audit_contract=VERSION)
        before = (directory / 'summary.json').read_bytes()
        with patch('sqlite3.connect', side_effect=AssertionError('No database allowed before scope check')):
            with self.assertRaises(ValueError):
                harness.run_evaluation(directory, generate=lambda *a, **k: self.fail('Unexpected generation'),
                                       audit_contract='legacy')
        self.assertEqual((directory / 'summary.json').read_bytes(), before)
        self.assertFalse((directory / 'studio.sqlite3').exists())

    def test_explicit_cli_prepare_records_contract_without_importing_model(self):
        program = ('import sys; from scripts.evaluate_assistant import main; '
                   'assert main(["--output-dir",sys.argv[1],"--audit-contract",'
                   '"scientific_defects_v1"])==0; '
                   'assert "grantlib.model" not in sys.modules; '
                   'assert "grantlib.studio" not in sys.modules')
        result = subprocess.run([sys.executable, '-c', program, str(self.root / 'cli')],
                                cwd=harness.REPO, capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads((self.root / 'cli' / 'summary.json').read_text())
        self.assertEqual(summary['audit_contract'], VERSION)
        self.assertEqual(summary['model_calls'], 0)

    def test_new_contract_requires_unchanged_prepared_fixture_before_opening_database(self):
        for change in ('alter_fixture', 'remove_fixture_binding'):
            with self.subTest(change=change):
                directory = harness.prepare(self.root / change, audit_contract=VERSION)
                summary_file = directory / 'summary.json'
                summary = json.loads(summary_file.read_text())
                self.assertEqual(summary['prepared_fixture_sha256'],
                                 hashlib.sha256((directory / 'fixture.json').read_bytes()).hexdigest())
                if change == 'alter_fixture':
                    fixture = json.loads((directory / 'fixture.json').read_text())
                    fixture['brief']['question'] += ' An unapproved but still fictional changed question.'
                    (directory / 'fixture.json').write_text(json.dumps(fixture))
                else:
                    del summary['prepared_fixture_sha256']
                    summary_file.write_text(json.dumps(summary))
                unchanged_receipt = summary_file.read_bytes()
                with patch('sqlite3.connect', side_effect=AssertionError('No database allowed before fixture guard')):
                    with self.assertRaises(ValueError):
                        harness.run_evaluation(directory, audit_contract=VERSION,
                            generate=lambda *a, **k: self.fail('Changed fixture reached generation'))
                self.assertEqual(summary_file.read_bytes(), unchanged_receipt)
                self.assertFalse((directory / 'studio.sqlite3').exists())

    def test_strategy_keeps_one_length_repair_when_new_contract_audit_is_invalid(self):
        directory = self.root / 'strategy'
        store = Store(directory)
        proposal = store.create({'title': 'Invented strategy', 'question': 'What is feasible?'})
        initial = {'title': 'Invented strategy', 'markdown': OBSERVATION + ' ' + 'Unresolved assumptions. ' * 20,
                   'claims': [], 'directions': [], 'open_questions': [], 'change_summary': ''}
        replacement = {**initial, 'markdown': SUBSTANTIVE}
        clean = {'contract_version': VERSION, 'summary': 'Injected limited check.',
                 'findings': [], 'limitations': []}
        responses = iter([initial, {**clean, 'contract_version': 'wrong'}, replacement, clean])
        calls = []

        def generate(prompt, **options):
            calls.append((prompt, deepcopy(options['schema'])))
            return {'status': 'completed', 'structured': next(responses), 'provider': 'injected'}

        service = Service(directory / 'unused-archive.sqlite3', generate=generate,
                          packet_builder=lambda *a: {'sources': []},
                          draft_audit_factory=audit_contract.prepare_draft,
                          revision_audit_factory=lambda *a, **k: self.fail('Wrong factory for strategy'))
        target = {'max_words': 22}
        job = store.queue(proposal['id'], proposal['revision'], 'strategy', length_target=target)
        target['max_words'] = 800  # The queued request, not this mutable caller object, governs.
        service._execute(job['id'], threading.Event())
        self.assertEqual(store.job(job['id'])['status'], 'completed')
        saved = store.get(proposal['id'])['artifacts'][0]
        self.assertEqual(saved['structured'], replacement)
        self.assertEqual(len(calls), 4)
        self.assertIn('DRAFT_SCIENTIFIC_DEFECTS_V1_QUOTES', calls[1][0])
        repair = json.loads(calls[2][0].split('CANDIDATE AND ATTRIBUTED MODEL FINDINGS (JSON):\n')[1])
        self.assertFalse(repair['scientific_check_available'])
        self.assertIsNone(repair['draft_check'])
        self.assertEqual(saved['model_metadata']['generation_request']['length_target']['max_words'], 22)
        self.assertEqual(saved['model_metadata']['length_check']['status'], 'within_target')
        check = saved['model_metadata']['draft_check']
        self.assertEqual(check['status'], 'incomplete')
        self.assertEqual(check['failed_stages'], ['check-initial'])
        self.assertEqual(check['report']['contract_version'], VERSION)
        self.assertEqual(check['repair_baseline'], draft_check.baseline_snapshot(initial))

    def test_experimental_audits_keep_cancellation_and_selected_memory_guards(self):
        for operation in ('cancel', 'retire_selected_memory'):
            for stage in ('check-initial', 'check-final'):
                with self.subTest(operation=operation, stage=stage):
                    directory = self.root / (operation + '-' + stage)
                    store = Store(directory)
                    proposal = store.create({'title': 'Invented safety boundary', 'question': 'What can be planned?'})
                    memory_record = {'category': 'capability', 'text': 'An invented core can perform masked imaging.',
                                     'scope': 'This fictional culture comparison only.', 'tags': [],
                                     'provenance_note': 'Invented investigator input for an isolated test.',
                                     'review_after': '', 'status': 'confirmed'}
                    if operation == 'retire_selected_memory':
                        catalog = store.create_memory(store.memory_catalog()['revision'], memory_record,
                                                      actor='investigator')
                        entry = catalog['entries'][0]
                        proposal = store.select_memory(proposal['id'], proposal['revision'], catalog['revision'],
                            [{'entry_id': entry['id'], 'version_id': entry['version_id'], 'note': 'Invented scope.'}])
                    candidate = {'title': 'Invented aims', 'markdown': SUBSTANTIVE, 'claims': [],
                                 'directions': [], 'open_questions': [], 'change_summary': ''}
                    clean = {'contract_version': VERSION, 'summary': 'Injected limited check.',
                             'findings': [], 'limitations': []}
                    issue = deepcopy(clean)
                    issue['findings'] = [{'id': 'routing_only', 'category': 'interpretation', 'severity': 'major',
                        'resolution': 'draft_repair', 'summary': 'Injected routing finding.',
                        'explanation': 'Exercise the next guarded stage.', 'repair': 'Retain conditional scope.',
                        'before': [], 'after': [{'kind': 'candidate', 'id': '', 'field': 'markdown',
                                                'quote': OBSERVATION}], 'authority': [], 'sources': []}]
                    calls = []

                    def generate(prompt, **options):
                        observed_stage = options['run_dir'].name
                        calls.append(observed_stage)
                        if observed_stage == stage:
                            if operation == 'cancel':
                                options['cancel_event'].set()
                            else:
                                store.revise_memory(entry['id'], store.memory_catalog()['revision'],
                                    {**memory_record, 'status': 'retired'}, actor='investigator')
                        if observed_stage.startswith('check-'):
                            self.assertTrue(prompt.startswith('DRAFT_SCIENTIFIC_DEFECTS_V1_QUOTES\n'))
                            value = issue if observed_stage == 'check-initial' else clean
                        else:
                            value = candidate
                        return {'status': 'completed', 'structured': deepcopy(value), 'provider': 'injected'}

                    service = Service(directory / 'unused-archive.sqlite3', generate=generate,
                                      packet_builder=lambda *a: {'sources': []},
                                      draft_audit_factory=audit_contract.prepare_draft)
                    job = store.queue(proposal['id'], proposal['revision'], 'aims')
                    service._execute(job['id'], threading.Event())
                    terminal = store.job(job['id'])
                    self.assertEqual(terminal['status'], 'cancelled' if operation == 'cancel' else 'failed')
                    self.assertEqual(store.get(proposal['id'])['artifacts'], [])
                    self.assertEqual(calls, ['draft', 'check-initial'] if stage == 'check-initial'
                                     else ['draft', 'check-initial', 'repair', 'check-final'])
                    if operation == 'retire_selected_memory':
                        self.assertIn('retired', terminal['error'])


if __name__ == '__main__':
    unittest.main()
