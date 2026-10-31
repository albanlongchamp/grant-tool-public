"""Real CLI/parser and local JSON command adapter, using invented content only.

The executable below is a deterministic Python fixture, not a model provider.
No network, credentials, private files or real archive are used.
"""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from grantlib.__main__ import main
from grantlib.harness import GrantHarness
from grantlib.model import ModelCancelled
from grantlib.studio import Store
from tests.test_harness import FictionalProvider, candidate, packet


FAKE_COMMAND = '''import json, pathlib, sys
request = json.load(sys.stdin)
assert request['protocol'] == 'grantlib.model.v1'
assert request['timeout_seconds'] > 0
with pathlib.Path(sys.argv[1]).open('a') as log:
    log.write(json.dumps({'protocol':request['protocol'], 'request_id':request['request_id'],
                         'requested_model':request['requested_model'], 'prompt':request['prompt'],
                         'schema':request['schema']}) + '\\n')
props = request['schema']['properties']
if 'findings' in props:
    value = {'summary':'An injected CLI review.', 'findings':[], 'limitations':[]}
    if 'commitments' in props:
        value['commitments'] = []
else:
    value = json.loads(sys.argv[2])
    if len(sys.argv) > 3 and sys.argv[3] == 'bad':
        value['claims'][0]['basis'] = 'malformed_fixture_basis'
print(json.dumps({'status':'completed', 'structured':value,
                  'provider':'fixture-command-observed', 'model':'observed-fixture-model',
                  'usage':{'input_tokens':1,'output_tokens':2}}))
'''


class HeadlessCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'state'
        self.archive = self.root / 'empty-archive'
        self.archive.mkdir()
        self.config = self.root / 'config.json'
        self.config.write_text(json.dumps({'data_dir': str(self.data),
            'sources': [{'name': 'fictional', 'path': str(self.archive)}]}))
        self.store = Store(self.data)
        self.script = self.root / 'fixture_provider.py'
        self.script.write_text(FAKE_COMMAND)
        self.log = self.root / 'fixture-requests.jsonl'
        self.adapter = self.root / 'adapter.json'
        self.write_adapter()
        retrieval = patch('grantlib.retrieval.build_packet', side_effect=packet)
        retrieval.start()
        self.addCleanup(retrieval.stop)
        for name in ('grantlib.model.generate', 'grantlib.model.availability'):
            guard = patch(name, side_effect=AssertionError('No real model/auth calls in headless CLI tests'))
            guard.start()
            self.addCleanup(guard.stop)

    def write_adapter(self, *, bad=False):
        command = [sys.executable, str(self.script), str(self.log), json.dumps(candidate())]
        if bad:
            command.append('bad')
        self.adapter.write_text(json.dumps({'provider': 'fixture-command-requested', 'command': command}))

    def proposal(self):
        return self.store.create({'title': 'Invented CLI proposal', 'question': 'An entirely fictional paired design.'})

    def cli(self, arguments):
        stdout, stderr, status = io.StringIO(), io.StringIO(), 0
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                main(['--config', str(self.config), 'proposal', *arguments])
            except SystemExit as exc:
                status = exc.code or 0
        return status, stdout.getvalue(), stderr.getvalue()

    def run_args(self, proposal, action='aims', *extra):
        return ['run', proposal['id'], '--action', action,
                '--expected-revision', str(proposal['revision']),
                '--provider', 'command', '--adapter-file', str(self.adapter),
                '--model', 'requested-fixture-model', *extra]

    def requests(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_normal_stdout_is_one_final_json_and_progress_only_stderr(self):
        p = self.proposal()
        status, out, err = self.cli(self.run_args(p, 'aims', '--min-words', '1', '--max-words', '100'))
        self.assertEqual(status, 0, err)
        job = json.loads(out)
        self.assertEqual(job['status'], 'completed')
        self.assertIn(job['id'], err)
        self.assertNotIn('PROPOSAL AND SOURCE DATA', err)
        requests = self.requests()
        self.assertEqual(len(requests), 2)
        self.assertEqual(len({request['request_id'] for request in requests}), 2)
        self.assertTrue(all(request['requested_model'] == 'requested-fixture-model' for request in requests))
        saved = self.store.get(p['id'])['artifacts'][0]
        self.assertEqual(saved['model_metadata']['requested_provider'], 'fixture-command-requested')
        self.assertEqual(saved['model_metadata']['requested_model'], 'requested-fixture-model')
        self.assertEqual(saved['model_metadata']['provider'], 'fixture-command-observed')
        self.assertEqual(saved['model_metadata']['model'], 'observed-fixture-model')
        self.assertEqual(saved['model_metadata']['length_check']['status'], 'within_target')

    def test_jsonl_success_has_only_typed_parseable_events(self):
        p = self.proposal()
        status, out, err = self.cli(self.run_args(p, 'design', '--jsonl'))
        self.assertEqual(status, 0, err)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(events[0]['type'], 'started')
        self.assertEqual(events[-1]['type'], 'result')
        self.assertTrue(all(event['type'] in {'started', 'phase', 'result'} for event in events))
        self.assertEqual(events[0]['job']['id'], events[-1]['job']['id'])
        self.assertEqual(events[-1]['job']['status'], 'completed')
        self.assertEqual(err, '')
        self.assertEqual(len(self.requests()), 2)

    def test_invalid_provider_response_is_failed_job_and_jsonl_error_without_retry(self):
        self.write_adapter(bad=True)
        p = self.proposal()
        status, out, err = self.cli(self.run_args(p, 'design', '--jsonl'))
        self.assertNotEqual(status, 0)
        events = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(events[-1]['type'], 'error')
        self.assertEqual(events[-1]['job']['status'], 'failed')
        self.assertNotIn('malformed_fixture_basis', out + err)
        self.assertEqual(len(self.requests()), 1)
        self.assertEqual(self.store.get(p['id'])['artifacts'], [])

    def test_revision_conflict_and_missing_parent_reject_before_command(self):
        p = self.proposal()
        changed = self.store.decision(p['id'], p['revision'], 'A synthetic investigator choice.')
        status, out, err = self.cli(self.run_args(p, 'design', '--jsonl'))
        self.assertNotEqual(status, 0)
        error = json.loads(out)
        self.assertEqual(error['type'], 'error')
        self.assertEqual(error['code'], 'conflict')
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.store.get(p['id']), changed)
        status, out, err = self.cli(self.run_args(changed, 'review', '--jsonl'))
        self.assertNotEqual(status, 0)
        self.assertEqual(json.loads(out)['type'], 'error')
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.store.get(p['id'])['jobs'], [])

    def test_job_and_cancel_commands_observe_existing_owner_without_new_provider(self):
        entered = threading.Event()
        def block(_prompt, options, _count):
            entered.set()
            options['cancel_event'].wait(3)
            raise ModelCancelled('Synthetic cancelled task.')
        provider = FictionalProvider(hook=block)
        h = GrantHarness(self.data / 'library.sqlite3', provider=provider, packet_builder=packet)
        self.addCleanup(h.close)
        p = self.proposal()
        job = h.start(p['id'], 'aims', expected_revision=p['revision'])
        self.assertTrue(entered.wait(2))
        status, out, err = self.cli(['job', job['id']])
        self.assertEqual(status, 0, err)
        self.assertEqual(json.loads(out)['id'], job['id'])
        status, out, err = self.cli(['cancel', job['id']])
        self.assertEqual(status, 0, err)
        self.assertTrue(json.loads(out)['cancel_requested'])
        terminal = h.wait(job['id'], timeout=3)
        self.assertEqual(terminal['status'], 'cancelled')
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.store.get(p['id'])['artifacts'], [])

    def test_evidence_command_never_invokes_a_provider_and_preserves_current_packet(self):
        p = self.proposal()
        status, out, err = self.cli(['evidence', p['id'], '--expected-revision', str(p['revision']), '--jsonl'])
        self.assertEqual(status, 0, err)
        events = [json.loads(line) for line in out.splitlines()]
        job = events[-1]['job']
        self.assertEqual(job['status'], 'completed')
        self.assertIsNone(job['result_artifact_id'])
        self.assertEqual(self.store.get(p['id'])['context']['sources'], packet()['sources'])
        self.assertEqual(self.requests(), [])

    def test_codex_model_selection_and_adapter_config_are_explicit(self):
        p = self.proposal()
        fake = FictionalProvider()
        with patch('grantlib.providers.CodexProvider', return_value=fake) as factory:
            status, out, err = self.cli(['run', p['id'], '--action', 'aims', '--provider', 'codex',
                                         '--model', 'explicit-fictional-model', '--jsonl'])
        self.assertEqual(status, 0, err)
        factory.assert_called_once_with(model='explicit-fictional-model')
        self.assertEqual(len(fake.calls), 2)
        current = self.store.get(p['id'])
        for values in (['--provider', 'command'], ['--provider', 'codex', '--adapter-file', str(self.adapter)]):
            status, out, err = self.cli(['run', current['id'], '--action', 'design', '--jsonl', *values])
            self.assertNotEqual(status, 0)
            self.assertEqual(json.loads(out)['type'], 'error')
        self.assertEqual(self.requests(), [])

    def test_stale_mutation_revision_does_not_silently_use_latest(self):
        p = self.proposal()
        changed = self.store.decision(p['id'], p['revision'], 'First synthetic choice.')
        status, out, err = self.cli(['decide', p['id'], '--text', 'Not authorized on changed state.',
                                   '--expected-revision', str(p['revision'])])
        self.assertNotEqual(status, 0)
        self.assertEqual(out, '')
        self.assertEqual(self.store.get(p['id']), changed)

    def test_job_command_recovers_dead_owner_without_launching_work(self):
        p = self.proposal()
        job = self.store.queue(p['id'], p['revision'], 'aims')
        with patch('grantlib.studio.os.kill', side_effect=ProcessLookupError):
            status, out, err = self.cli(['job', job['id']])
        self.assertEqual(status, 0, err)
        self.assertEqual(json.loads(out)['status'], 'failed')
        self.assertEqual(self.requests(), [])
        self.assertEqual(self.store.get(p['id'])['artifacts'], [])
