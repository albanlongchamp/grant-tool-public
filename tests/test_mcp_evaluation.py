"""Fictional paired-client evaluation and rubric isolation, without inference."""
import asyncio
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import evaluate_mcp_workflow as evaluation

HAS_MCP = importlib.util.find_spec('mcp') is not None


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'evaluation'

    def test_prepare_has_no_calls_and_rejects_overwrite(self):
        with patch('grantlib.model.generate', side_effect=AssertionError('No inference')):
            evaluation.prepare(self.path)
        summary = json.loads((self.path / 'summary.json').read_text())
        self.assertEqual(summary['status'], 'prepared')
        self.assertEqual(summary['model_calls'], 0)
        fixture = json.loads((self.path / 'fixture.json').read_text())
        self.assertEqual(len(fixture['cases']), 3)
        with self.assertRaises(FileExistsError):
            evaluation.prepare(self.path)

    def test_model_input_excludes_every_rubric_and_preserves_evidence(self):
        fixture = json.loads(evaluation.FIXTURE.read_text())
        for case in fixture['cases']:
            payload = evaluation.model_input(case)
            wire = json.dumps(payload)
            self.assertEqual(payload['sources'], case['sources'])
            self.assertNotIn('reviewer_criteria', payload)
            self.assertNotIn('EVAL_ONLY_', wire)
            for criterion in case['reviewer_criteria']:
                self.assertNotIn(criterion, wire)
            payload['sources'][0]['text'] = 'changed'
            self.assertNotEqual(payload['sources'], case['sources'])

    def test_metrics_do_not_claim_scientific_support(self):
        result = evaluation.sanity_metrics('A claim [[P_present]]. Another [[P_missing]].', ['P_present'])
        self.assertEqual(result['unknown_citation_ids'], ['P_missing'])
        self.assertEqual(result['scientific_quality'], 'not_scored')
        self.assertEqual(result['review_status'], 'independent_review_pending')

    def test_tampered_prepared_fixture_rejected_before_responder(self):
        evaluation.prepare(self.path)
        fixture = json.loads((self.path / 'fixture.json').read_text())
        fixture['cases'][0]['task'] = 'changed'
        (self.path / 'fixture.json').write_text(json.dumps(fixture))
        with self.assertRaisesRegex(ValueError, 'unchanged'):
            asyncio.run(evaluation.run_evaluation(self.path, responder=lambda request: self.fail('No model call'),
                        mode='injected', requested_model='fixture'))


@unittest.skipUnless(HAS_MCP, 'Install the MCP extra for real-protocol evaluation replay.')
class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_real_protocol_replay_is_fictional_bounded_and_blinded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = evaluation.prepare(Path(folder) / 'evaluation')
            observed = []
            def respond(request):
                observed.append(request)
                return evaluation.replay_responder(request)
            with ExitStack() as stack:
                for target in ('grantlib.model.generate', 'grantlib.model.availability', 'socket.create_connection', 'socket.socket.connect'):
                    stack.enter_context(patch(target, side_effect=AssertionError('No model, auth or network')))
                summary = await evaluation.run_evaluation(path, responder=respond, mode='injected', requested_model='fixture')
            self.assertEqual(summary['status'], 'completed')
            self.assertEqual(summary['mode'], 'injected')
            self.assertEqual(summary['server_model_calls'], 0)
            self.assertEqual(summary['quality_advantage'], 'not_established')
            self.assertEqual(summary['model_calls'], len(observed))
            self.assertEqual(summary['model_call_attempts'], len(observed))
            self.assertEqual(len(summary['arm_results']), 6)
            for request in observed:
                self.assertNotIn('EVAL_ONLY_', json.dumps(request))
                self.assertNotIn('reviewer_criteria', json.dumps(request))
                self.assertTrue({t['name'] for t in request['tools']} <= evaluation.ALLOWED_TOOLS)
                if request['arm'] == 'plain':
                    self.assertEqual(request['tools'], [])
            results = json.loads((path / 'results.json').read_text())
            by_case = {}
            for result in results:
                by_case.setdefault(result['case_id'], []).append(result['scientific_input_sha256'])
                self.assertFalse(result['usage']['cost_complete'])
                for stage in result['stages']:
                    persisted_trace = json.loads((path / result['case_id'] / result['arm'] / stage['stage'] / 'tool-trace.json').read_text())
                    self.assertEqual(persisted_trace, stage['tool_trace'])
                    self.assertEqual(stage['mechanical_metrics']['unknown_citation_ids'], [])
                    if result['arm'] == 'mcp':
                        self.assertTrue(stage['workflow_checks']['saved_text_matches_final'])
                        if stage['stage'] == 'revision':
                            self.assertTrue(stage['workflow_checks']['recorded_update'])
                        self.assertIn('grants_get_review_queue', [c['name'] for c in stage['tool_trace']])
                        self.assertIn('evaluator_verification', {c['origin'] for c in stage['tool_trace']})
            self.assertTrue(all(len(set(values)) == 1 for values in by_case.values()))
            reviews = json.loads((path / 'reviewer_packet.json').read_text())
            keys = json.loads((path / 'unblinding_key.json').read_text())
            self.assertEqual(len(reviews), 12)
            self.assertEqual({r['review_id'] for r in reviews}, {r['review_id'] for r in keys})
            self.assertTrue(all('arm' not in r and r['ratings']['design_errors'] is None for r in reviews))
            result_map = {(r['case_id'], r['arm']): r for r in results}
            key_map = {k['review_id']: k for k in keys}
            fixture_cases = {c['id']: c for c in json.loads(evaluation.FIXTURE.read_text())['cases']}
            for review in reviews:
                case = fixture_cases[review['case_id']]
                self.assertEqual(review['brief'], case['brief'])
                self.assertEqual(review['original_task'], case['task'])
                if review['stage'] == 'revision':
                    key = key_map[review['review_id']]
                    parent = result_map[(key['case_id'], key['arm'])]['stages'][0]['text']
                    self.assertEqual(review['parent_text'], parent)
                    self.assertEqual(review['change_request'], case['change'])
                else:
                    self.assertIsNone(review['parent_text'])
                    self.assertIsNone(review['change_request'])
                self.assertFalse(set(review) & {'tool_trace', 'observed_model', 'model', 'calls', 'usage'})
            # Upgrade an older reviewer packet without changing outputs or receipts.
            immutable = {p: p.read_bytes() for p in path.rglob('*.json')
                         if p.name != 'reviewer_packet.json'}
            old = [{key: value for key, value in r.items()
                    if key not in {'brief', 'original_task', 'change_request', 'parent_text'}} for r in reviews]
            (path / 'reviewer_packet.json').write_text(json.dumps(old))
            receipt = evaluation.enrich_reviewer_packet(path)
            self.assertFalse(receipt['model_outputs_or_receipts_changed'])
            self.assertEqual(json.loads((path / 'reviewer_packet.json').read_text()), reviews)
            self.assertTrue(all(p.read_bytes() == content for p, content in immutable.items()))
            with self.assertRaisesRegex(ValueError, 'newly prepared'):
                await evaluation.run_evaluation(path, responder=respond, mode='injected', requested_model='fixture')

    async def test_plain_arm_cannot_call_tools_and_failed_run_is_labeled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = evaluation.prepare(Path(folder) / 'evaluation')
            def call_tool(request):
                return {'text': None, 'tool_calls': [{'id': 'x', 'name': 'grants_get_proposal', 'arguments_json': '{}'}]}
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                await evaluation.run_evaluation(path, responder=call_tool, mode='injected', requested_model='fixture')
            summary = json.loads((path / 'summary.json').read_text())
            self.assertEqual(summary['status'], 'failed')
            self.assertEqual(summary['model_call_attempts'], 1)
            self.assertEqual(summary['model_calls'], 1)
            self.assertEqual(summary['quality_advantage'], 'not_established')

    async def test_failed_responder_records_attempt_without_inventing_completion(self):
        with tempfile.TemporaryDirectory() as folder:
            path = evaluation.prepare(Path(folder) / 'evaluation')
            def fail(request):
                raise TimeoutError('Fictional timeout')
            with self.assertRaises(TimeoutError):
                await evaluation.run_evaluation(path, responder=fail, mode='injected', requested_model='fixture')
            summary = json.loads((path / 'summary.json').read_text())
            self.assertEqual(summary['model_call_attempts'], 1)
            self.assertEqual(summary['model_calls'], 0)
            receipt = json.loads(next(path.rglob('call-01.failed.json')).read_text())
            self.assertIn('unknown', receipt['inference_completion'])
            self.assertEqual(receipt['error_type'], 'TimeoutError')
            with self.assertRaisesRegex(ValueError, 'completed run'):
                evaluation.enrich_reviewer_packet(path)


if __name__ == '__main__':
    unittest.main()
