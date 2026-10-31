"""Model protocol tests use real short-lived synthetic child processes."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from grantlib import model


SCHEMA = {"type": "object", "properties": {"summary": {"type": "string", "minLength": 3}},
          "required": ["summary"], "additionalProperties": False}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.real_popen = subprocess.Popen
        self.commands = []
        self.children = []

    def tearDown(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
                child.wait()
        self.temp.cleanup()

    def fake_cli(self, script):
        def launch(args, **kwargs):
            self.commands.append(args)
            child = self.real_popen([sys.executable, "-c", script, *args[1:]], **kwargs)
            self.children.append(child)
            return child
        return patch("grantlib.model.subprocess.Popen", side_effect=launch)

    def completed_script(self, output):
        return f'''
import json,sys
from pathlib import Path
prompt=sys.stdin.read()
print(json.dumps({{"type":"thread.started","thread_id":"synthetic-thread","model":"test-model"}}),flush=True)
print(json.dumps({{"type":"turn.started"}}),flush=True)
print(json.dumps({{"type":"item.completed","item":{{"type":"reasoning","text":prompt}}}}),flush=True)
Path(sys.argv[sys.argv.index('-o')+1]).write_text({output!r})
print(json.dumps({{"type":"item.completed","item":{{"type":"agent_message","text":{output!r}}}}}),flush=True)
print(json.dumps({{"type":"turn.completed","usage":{{"input_tokens":20,"output_tokens":5}}}}),flush=True)
'''

    @patch("grantlib.model._binary", return_value="codex")
    def test_completed_result_is_structured_and_private_context_is_not_logged(self, _):
        events = []
        with self.fake_cli(self.completed_script('{"summary":"A proposed experiment."}')):
            result = model.generate("PRIVATE_SOURCE_SENTINEL", run_dir=self.root, schema=SCHEMA, on_event=events.append)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["structured"], {"summary": "A proposed experiment."})
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["usage"]["output_tokens"], 5)
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", json.dumps(events))
        directory = self.root / result["run_id"]
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", (directory / "metadata.json").read_text())
        args = self.commands[0]
        self.assertNotIn("PRIVATE_SOURCE_SENTINEL", " ".join(args))
        self.assertIn("--ignore-user-config", args)
        self.assertIn("--ephemeral", args)
        self.assertIn('default_permissions="grant_text"', args)
        self.assertIn('permissions.grant_text.network.enabled=false', args)
        permissions = next(x for x in args if x.startswith('permissions.grant_text.filesystem='))
        self.assertIn(str(directory), permissions)
        self.assertNotIn('"write"', permissions)
        self.assertIn('web_search="disabled"', args)
        self.assertIn("shell_tool", args)
        disabled = {args[index + 1] for index, item in enumerate(args[:-1]) if item == '--disable'}
        self.assertIn('code_mode', disabled)
        self.assertNotIn('code_mode_host', disabled)
        self.assertNotIn("--ignore-rules", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)
        self.assertTrue(all(child.poll() == 0 for child in self.children))

    @patch("grantlib.model._binary", return_value="codex")
    def test_explicit_model_is_separate_from_observed_identity(self, _):
        with self.fake_cli(self.completed_script('{"summary":"A proposed experiment."}')):
            result = model.generate('Synthetic model selection', run_dir=self.root,
                                    schema=SCHEMA, model='requested-frontier')
        args = self.commands[0]
        self.assertEqual(args[args.index('--model') + 1], 'requested-frontier')
        self.assertEqual(result['requested_provider'], 'codex')
        self.assertEqual(result['requested_model'], 'requested-frontier')
        self.assertEqual(result['model'], 'test-model')
        with patch('grantlib.model.subprocess.Popen') as process:
            for invalid in ('', 'two model names', 'bad\nname', 7):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    model.generate('Synthetic task', run_dir=self.root, model=invalid)
            process.assert_not_called()

    @patch("grantlib.model._binary", return_value="codex")
    def test_early_completion_rejects_incomplete_prompt_delivery(self, _):
        script = self.completed_script('{"summary":"A proposed experiment."}').replace(
            'prompt=sys.stdin.read()', 'prompt=""')
        with self.fake_cli(script), self.assertRaises(model.ModelOutputError):
            model.generate('x' * 1_000_000, run_dir=self.root, schema=SCHEMA)
        self.assertEqual(len(self.children), 1)
        self.assertTrue(all(child.poll() is not None for child in self.children))
        metadata = json.loads(next(self.root.glob('model-*/metadata.json')).read_text())
        self.assertEqual(metadata['status'], 'invalid_output')
        self.assertEqual(metadata['error_code'], 'invalid_output')

    @patch("grantlib.model._binary", return_value="codex")
    def test_schema_violation_is_not_accepted_as_a_draft(self, _):
        with self.fake_cli(self.completed_script('{"summary":7}')):
            with self.assertRaises(model.ModelSchemaError) as error:
                model.generate("Synthetic task", run_dir=self.root, schema=SCHEMA)
        self.assertNotIsInstance(error.exception, model.ModelRequestSchemaError)
        meta = json.loads(next(self.root.glob("model-*/metadata.json")).read_text())
        self.assertEqual(meta["status"], "schema_error")
        self.assertEqual(meta["process_exit_code"], 0)
        self.assertNotIn("rejected_keywords", meta)

    @patch("grantlib.model._binary", return_value="codex")
    def test_provider_schema_rejection_is_distinct_redacted_and_does_not_change_schema(self, _):
        script = '''
import json,sys
prompt=sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"synthetic-thread"}),flush=True)
print(json.dumps({"type":"turn.started"}),flush=True)
print(json.dumps({"type":"turn.failed","error":{"message":
    "Invalid schema for response_format 'codex_schema': In context=('properties', 'PRIVATE_FIELD_SENTINEL'), "
    "'uniqueItems' is not permitted. SECRET_TOKEN_SENTINEL "+prompt}}),flush=True)
print("SECRET_STDERR_SENTINEL",file=sys.stderr,flush=True)
sys.exit(1)
'''
        schema = {"type":"array","items":{"type":"string"},"uniqueItems":True}
        events = []
        with self.fake_cli(script):
            with self.assertRaises(model.ModelRequestSchemaError) as error:
                model.generate("PRIVATE_PROMPT_SENTINEL", run_dir=self.root, schema=schema, on_event=events.append)
        self.assertNotIsInstance(error.exception, model.ModelSchemaError)
        self.assertEqual(error.exception.code, "request_schema_error")
        self.assertEqual(error.exception.rejected_keywords, ("uniqueItems",))
        self.assertEqual(error.exception.process_exit_code, 1)
        directory = next(self.root.glob("model-*"))
        meta = json.loads((directory / "metadata.json").read_text())
        self.assertEqual(meta["status"], "request_schema_error")
        self.assertEqual(meta["error_code"], "request_schema_error")
        self.assertEqual(meta["rejected_keywords"], ["uniqueItems"])
        self.assertEqual(meta["process_exit_code"], 1)
        self.assertEqual(json.loads((directory / "schema.json").read_text()), schema)
        self.assertFalse((directory / "final.txt").exists())
        persisted = json.dumps(meta) + json.dumps(events) + str(error.exception) + repr(vars(error.exception))
        for sentinel in ("PRIVATE_FIELD_SENTINEL", "SECRET_TOKEN_SENTINEL", "SECRET_STDERR_SENTINEL", "PRIVATE_PROMPT_SENTINEL"):
            self.assertNotIn(sentinel, persisted)
        self.assertEqual(len(self.children), 1)
        self.assertEqual(self.children[0].poll(), 1)

    @patch("grantlib.model._binary", return_value="codex")
    def test_unknown_rejected_schema_name_is_not_retained(self, _):
        script = '''
import json,sys
sys.stdin.read()
print(json.dumps({"type":"error","message":
    "Invalid schema for response_format 'codex_schema': 'PRIVATE_KEY_SENTINEL' is not permitted."}),flush=True)
sys.exit(1)
'''
        with self.fake_cli(script):
            with self.assertRaises(model.ModelRequestSchemaError) as error:
                model.generate("Synthetic task", run_dir=self.root, schema=SCHEMA)
        self.assertEqual(error.exception.rejected_keywords, ())
        meta = json.loads(next(self.root.glob("model-*/metadata.json")).read_text())
        self.assertEqual(meta["rejected_keywords"], [])
        self.assertNotIn("PRIVATE_KEY_SENTINEL", json.dumps(meta) + str(error.exception))

    @patch("grantlib.model._binary", return_value="codex")
    def test_network_failure_with_schema_mention_stays_unknown_and_redacted(self, _):
        script = '''
import json,sys
sys.stdin.read()
print(json.dumps({"type":"turn.failed","error":{"message":
    "Connection closed while sending schema with uniqueItems: SECRET_NETWORK_SENTINEL"}}),flush=True)
sys.exit(7)
'''
        events = []
        with self.fake_cli(script):
            with self.assertRaises(model.ModelError) as error:
                model.generate("Synthetic task", run_dir=self.root, schema=SCHEMA, on_event=events.append)
        self.assertIs(type(error.exception), model.ModelError)
        meta = json.loads(next(self.root.glob("model-*/metadata.json")).read_text())
        self.assertEqual(meta["status"], "model_error")
        self.assertEqual(meta["process_exit_code"], 7)
        self.assertNotIn("rejected_keywords", meta)
        self.assertNotIn("SECRET_NETWORK_SENTINEL", str(error.exception) + json.dumps(meta) + json.dumps(events))

    def test_request_schema_classifier_requires_explicit_rejection_and_keyword(self):
        self.assertIsNone(model._request_schema_rejection("Please check the schema after a transport error."))
        self.assertIsNone(model._request_schema_rejection("Generated output does not match schema."))
        self.assertEqual(model._request_schema_rejection(
            'Invalid schema for response_format: mentions uniqueItems but does not reject it.'), [])
        self.assertEqual(model._request_schema_rejection(
            json.dumps({"code":"invalid_json_schema","message":'"uniqueItems" is not permitted; const is not supported.'})),
            ["const", "uniqueItems"])
        self.assertEqual(model._request_schema_rejection(
            'Invalid schema for text.format: unsupported schema keyword: uniqueItems'), ["uniqueItems"])

    @patch("grantlib.model._binary", return_value="codex")
    def test_failed_turn_and_auth_details_are_redacted(self, _):
        script = '''
import json,sys
sys.stdin.read()
print(json.dumps({"type":"turn.failed","error":{"message":"Unauthorized 401 SECRET_TOKEN_SENTINEL"}}),flush=True)
sys.exit(1)
'''
        with self.fake_cli(script):
            with self.assertRaises(model.ModelAuthenticationError) as error:
                model.generate("Synthetic task", run_dir=self.root)
        self.assertNotIn("SECRET_TOKEN_SENTINEL", str(error.exception))
        self.assertNotIn("SECRET_TOKEN_SENTINEL", next(self.root.glob("model-*/metadata.json")).read_text())

    @patch("grantlib.model._binary", return_value="codex")
    def test_exit_zero_without_completed_event_is_not_success(self, _):
        script = 'import json,sys; sys.stdin.read(); print(json.dumps({"type":"thread.started","thread_id":"x"}))'
        with self.fake_cli(script):
            with self.assertRaises(model.ModelOutputError):
                model.generate("Synthetic task", run_dir=self.root)

    @patch("grantlib.model._binary", return_value="codex")
    def test_tool_event_is_rejected_and_child_reaped(self, _):
        script = '''
import json,sys,time
sys.stdin.read()
print(json.dumps({"type":"item.started","item":{"type":"command_execution","command":"unsafe"}}),flush=True)
time.sleep(10)
'''
        with self.fake_cli(script):
            with self.assertRaises(model.ModelOutputError):
                model.generate("Synthetic task", run_dir=self.root)
        self.assertIsNotNone(self.children[0].poll())

    @patch("grantlib.model._binary", return_value="codex")
    def test_timeout_and_cancellation_reap_the_process(self, _):
        script = 'import sys,time; sys.stdin.read(); time.sleep(20)'
        with self.fake_cli(script):
            with self.assertRaises(model.ModelTimeout):
                model.generate("Synthetic task", run_dir=self.root, timeout=0.1)
        self.assertIsNotNone(self.children[0].poll())
        cancel = threading.Event()
        timer = threading.Timer(0.1, cancel.set)
        timer.start()
        try:
            with self.fake_cli(script):
                with self.assertRaises(model.ModelCancelled):
                    model.generate("Synthetic task", run_dir=self.root, cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertIsNotNone(self.children[-1].poll())

    @patch("grantlib.model._binary", return_value="codex")
    def test_large_stdin_does_not_deadlock_cancellation(self, _):
        script = 'import time; time.sleep(20)'
        with self.fake_cli(script):
            with self.assertRaises(model.ModelTimeout):
                model.generate("x" * 1_000_000, run_dir=self.root, timeout=0.15)
        self.assertIsNotNone(self.children[0].poll())

    def test_invalid_schema_is_rejected_before_starting_a_process(self):
        with patch("grantlib.model.subprocess.Popen") as process:
            with self.assertRaises(model.ModelSchemaError):
                model.generate("Synthetic task", run_dir=self.root, schema={"type": "string", "format": "email"})
            process.assert_not_called()

    def test_nested_schema_references_and_constraints(self):
        schema = {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/item"},
                  "$defs": {"item": {"type": "object", "properties": {"kind": {"enum": ["proposed", "observed"]}},
                                     "required": ["kind"], "additionalProperties": False}}}
        model._check_schema(schema)
        model._validate([{"kind": "proposed"}], schema)
        for invalid in ([], [{"kind": "fabricated"}], [{"kind": "proposed", "extra": 2}]):
            with self.assertRaises(model.ModelSchemaError):
                model._validate(invalid, schema)

    def test_schema_numeric_equality_does_not_accept_booleans(self):
        with self.assertRaises(model.ModelSchemaError):
            model._validate(True, {"enum": [1]})
        model._validate(1.0, {"enum": [1]})
        with self.assertRaises(model.ModelSchemaError):
            model._validate([1, 1.0], {"type": "array", "uniqueItems": True})
        model._validate([True, 1], {"type": "array", "uniqueItems": True})

    @patch("grantlib.model._binary", return_value="codex")
    def test_malformed_event_is_rejected(self, _):
        with self.fake_cli('import sys; sys.stdin.read(); print("[]")'):
            with self.assertRaises(model.ModelOutputError):
                model.generate("Synthetic task", run_dir=self.root)

    def test_availability_only_reports_local_auth_status(self):
        responses = [subprocess.CompletedProcess([], 0, "codex-cli 0.166.0\n", ""),
                     subprocess.CompletedProcess([], 0, "--ignore-user-config", ""),
                     subprocess.CompletedProcess([], 0, "", "Logged in using ChatGPT\n")]
        with patch("grantlib.model.shutil.which", return_value="codex"), patch("grantlib.model.subprocess.run", side_effect=responses) as run:
            result = model.availability()
        self.assertTrue(result["available"])
        self.assertEqual(result["auth_method"], "chatgpt")
        self.assertEqual(result["version"], "0.166.0")
        self.assertTrue(all("exec" not in c.args[0] or "--help" in c.args[0] for c in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
