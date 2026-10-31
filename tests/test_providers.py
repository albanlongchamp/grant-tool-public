"""Provider boundaries with injected Python callbacks and synthetic subprocesses."""
from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from grantlib import model, providers
from grantlib.studio import Conflict


SCHEMA = {'type': 'object', 'properties': {'claims': {'type': 'array', 'items': {
    'type': 'object', 'properties': {'text': {'type': 'string', 'minLength': 3}},
    'required': ['text'], 'additionalProperties': False}}},
    'required': ['claims'], 'additionalProperties': False}
VALUE = {'claims': [{'text': 'An invented proposal.'}]}


def completion(**changes):
    return {'status': 'completed', 'structured': deepcopy(VALUE), 'provider': 'observed-provider',
            'model': None, 'usage': {'input_tokens': 10, 'output_tokens': 4, 'cached_input_tokens': 3}, **changes}


class CheckedProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_provider(self, provider, **kwargs):
        return providers.checked_generator(provider)('PRIVATE_PROMPT_SENTINEL', run_dir=self.root,
                                                     schema=deepcopy(SCHEMA), **kwargs)

    def test_callable_forms_and_requested_identity_are_not_observed_identity(self):
        def adapter(prompt, **kwargs):
            return completion(requested_provider='spoofed', requested_model='spoofed')
        adapter.requested_provider, adapter.requested_model = 'configured', 'requested-frontier'
        checked = providers.checked_generator(adapter)
        adapter.requested_provider = 'later-mutated'
        result = checked('Synthetic task', run_dir=self.root, schema=SCHEMA)
        self.assertEqual(result['requested_provider'], 'configured')
        self.assertEqual(result['requested_model'], 'requested-frontier')
        self.assertEqual(result['provider'], 'observed-provider')
        self.assertIsNone(result['model'])
        self.assertEqual(json.loads(result['text']), VALUE)
        for form in (completion(text=json.dumps(VALUE)), completion(structured=None, text=json.dumps(VALUE))):
            actual = self.run_provider(lambda *a, **k: form)
            self.assertEqual(actual['structured'], VALUE)
            self.assertEqual(actual['requested_provider'], 'python_callable')

    def test_original_schema_is_frozen_and_nested_schema_is_enforced(self):
        schema = deepcopy(SCHEMA)
        def adapter(prompt, **kwargs):
            kwargs['schema'].clear()
            schema.clear()
            return completion(structured={'claims': [{'text': 7}]})
        with self.assertRaises(model.ModelSchemaError):
            providers.checked_generator(adapter)('Synthetic task', run_dir=self.root, schema=schema)

    def test_invalid_partial_mismatched_tool_and_nonfinite_results_are_rejected(self):
        values = [None, [], completion(status='partial'), completion(structured=None, text=''),
                  completion(text='{"claims":[]}'), completion(structured=[], text=None),
                  completion(tool_calls=[{'name': 'read_private_file'}]), completion(usage={'tokens': float('nan')}),
                  completion(usage={'tokens': True}), completion(usage={'tokens': -1}),
                  completion(text='{"claims":[],"claims":[{"text":"An invented proposal."}]}'),
                  completion(provider='secret\nmetadata'), completion(elapsed_seconds=float('inf'))]
        for value in values:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(model.ModelOutputError):
                self.run_provider(lambda *a, **k: value)
        with patch.object(providers, 'MAX_RESPONSE_BYTES', 10), self.assertRaises(model.ModelOutputError):
            self.run_provider(lambda *a, **k: completion())

    def test_errors_and_progress_do_not_leak_raw_messages(self):
        events = []
        def adapter(prompt, **kwargs):
            kwargs['on_event']({'type': 'started', 'run_id': 'run-test', 'prompt': prompt, 'stderr': 'SECRET'})
            kwargs['on_event']({'type': 'reasoning', 'text': prompt})
            raise RuntimeError('SECRET ' + prompt)
        with self.assertRaises(model.ModelError) as caught:
            self.run_provider(adapter, on_event=events.append)
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertNotIn('PRIVATE_PROMPT_SENTINEL', str(caught.exception))
        self.assertEqual(events, [{'type': 'started', 'run_id': 'run-test'}])
        def stale(*a, **k):
            raise Conflict('PRIVATE_PROMPT_SENTINEL')
        with self.assertRaises(Conflict) as stale_error:
            self.run_provider(stale)
        self.assertNotIn('PRIVATE_PROMPT_SENTINEL', str(stale_error.exception))

    def test_cancel_and_deadline_reject_returned_result_without_retry(self):
        cancel = threading.Event()
        calls = []
        def adapter(*a, **k):
            calls.append(1)
            cancel.set()
            return completion()
        with self.assertRaises(model.ModelCancelled): self.run_provider(adapter, cancel_event=cancel)
        with self.assertRaises(model.ModelCancelled): self.run_provider(adapter, cancel_event=cancel)
        self.assertEqual(len(calls), 1)
        def slow(*a, **k):
            calls.append(1)
            time.sleep(.03)
            return completion()
        with self.assertRaises(model.ModelTimeout): self.run_provider(slow, timeout=.01)
        self.assertEqual(len(calls), 2)

    def test_codex_selection_delegates_without_login_or_provider_fallback(self):
        provider = providers.CodexProvider(model='requested-frontier')
        with patch('grantlib.model.generate', return_value=completion()) as generate:
            result = self.run_provider(provider)
        self.assertEqual(generate.call_args.kwargs['model'], 'requested-frontier')
        self.assertEqual(result['requested_provider'], 'codex')
        self.assertEqual(result['requested_model'], 'requested-frontier')
        with self.assertRaises(FrozenInstanceError): provider.model = 'changed'


class CommandProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.children = []
        self.real_popen = subprocess.Popen
        def launch(*args, **kwargs):
            child = self.real_popen(*args, **kwargs)
            self.children.append(child)
            return child
        self.launch = patch('grantlib.providers.subprocess.Popen', side_effect=launch)
        self.launch.start()
        self.addCleanup(self.launch.stop)
        self.addCleanup(self.reap)

    def reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()

    def adapter(self, script, **kwargs):
        return providers.CommandProvider([sys.executable, '-c', script], **kwargs)

    def run_provider(self, adapter, prompt='PRIVATE_PROMPT_SENTINEL', **kwargs):
        return providers.checked_generator(adapter)(prompt, run_dir=self.root, schema=SCHEMA, **kwargs)

    def test_actual_child_protocol_environment_private_files_and_safe_metadata(self):
        script = '''
import json,os,sys
request=json.load(sys.stdin)
assert request['protocol']=='grantlib.model.v1'
assert request['prompt']=='PRIVATE_PROMPT_SENTINEL'
assert request['requested_model']=='chosen-frontier'
assert request['schema']['properties']['claims']['type']=='array'
assert request['timeout_seconds']>0
assert os.environ['FICTIONAL_AUTH_SENTINEL']=='secret-fixture'
print('SECRET_STDERR '+request['prompt'],file=sys.stderr)
print(json.dumps({'status':'completed','structured':{'claims':[{'text':'An invented proposal.'}]},'provider':'reported-adapter','model':'observed-version','usage':{'input_tokens':4}}))
'''
        with patch.dict('os.environ', {'FICTIONAL_AUTH_SENTINEL': 'secret-fixture'}):
            result = self.run_provider(self.adapter(script, provider='configured-adapter', model='chosen-frontier'))
        self.assertEqual(result['structured'], VALUE)
        self.assertEqual(result['provider'], 'reported-adapter')
        self.assertEqual(result['requested_provider'], 'configured-adapter')
        self.assertEqual(result['model'], 'observed-version')
        directory = self.root / result['run_id']
        self.assertTrue(directory.is_dir())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        metadata = (directory / 'metadata.json').read_text()
        for secret in ('PRIVATE_PROMPT_SENTINEL', 'SECRET_STDERR', 'secret-fixture'):
            self.assertNotIn(secret, metadata)
        self.assertEqual((directory / 'completion.json').stat().st_mode & 0o777, 0o600)
        self.assertTrue(all(p.poll() == 0 for p in self.children))

    def test_failures_do_not_retry_or_expose_stderr_or_accept_partial_tool_output(self):
        scripts = ["import sys; sys.stdin.read(); print('SECRET_STDERR', file=sys.stderr); sys.exit(7)",
                   "import sys; sys.stdin.read(); print('SECRET_NOT_JSON')",
                   "import sys; sys.stdin.read(); print('{} {}')",
                   "import sys; sys.stdin.read(); print('" + json.dumps(completion(status='partial')) + "')",
                   "import sys; sys.stdin.read(); print('" + json.dumps(completion(tool_calls=[])) + "')"]
        for script in scripts:
            count = len(self.children)
            with self.assertRaises(model.ModelError) as caught:
                self.run_provider(self.adapter(script))
            self.assertEqual(len(self.children), count + 1)
            self.assertNotIn('SECRET', str(caught.exception))
        self.assertTrue(all(child.poll() is not None for child in self.children))
        self.assertNotIn('SECRET', '\n'.join(p.read_text() for p in self.root.glob('command-*/metadata.json')))

    def test_nonblocking_large_stdin_cancellation_and_timeout_reap_children(self):
        adapter = self.adapter('import time; time.sleep(10)')
        with self.assertRaises(model.ModelTimeout): self.run_provider(adapter, prompt='x' * 1_000_000, timeout=.1)
        cancel = threading.Event()
        timer = threading.Timer(.1, cancel.set)
        timer.start()
        try:
            with self.assertRaises(model.ModelCancelled): self.run_provider(adapter, cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertTrue(all(child.poll() is not None for child in self.children))

    def test_early_completion_cannot_hide_incomplete_stdin_delivery(self):
        script = 'import json; print(' + repr(json.dumps(completion())) + ', flush=True)'
        with self.assertRaises(model.ModelOutputError):
            self.run_provider(self.adapter(script), prompt='x' * 1_000_000)
        self.assertEqual(len(self.children), 1)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        metadata = json.loads(next(self.root.glob('command-*/metadata.json')).read_text())
        self.assertEqual(metadata['status'], 'invalid_output')
        self.assertFalse(list(self.root.glob('command-*/completion.json')))

    def test_response_and_stderr_bounds_and_argument_configuration(self):
        for script in ('import sys; sys.stdin.read(); print("x"*1000)',
                       'import sys; sys.stdin.read(); print("x"*1000,file=sys.stderr)'):
            with patch.object(providers, 'MAX_RESPONSE_BYTES', 500), patch.object(providers, 'MAX_STDERR_BYTES', 500):
                with self.assertRaises(model.ModelOutputError): self.run_provider(self.adapter(script))
        for command in ('echo unsafe shell', [], [''], [sys.executable, '\0']):
            with self.assertRaises(ValueError): providers.CommandProvider(command)
        provider = self.adapter('pass')
        with self.assertRaises(FrozenInstanceError): provider.requested_model = 'changed'


if __name__ == '__main__':
    unittest.main()
