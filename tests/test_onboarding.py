"""First-use MCP intake with invented text; no home scans, model or network."""
from contextlib import ExitStack
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grantlib import library
from grantlib.mcp_server import GrantTools, create_server
from grantlib.onboarding import open_workspace
HAS_MCP = importlib.util.find_spec('mcp') is not None
if HAS_MCP:
    from mcp import Client


def forbidden(*args, **kwargs):
    raise AssertionError('Intake must never contact a model or network.')


class OnboardingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.local.json'
        self.harness = open_workspace(self.config, provider=forbidden)
        self.addCleanup(self.harness.close)
        self.tools = GrantTools(self.harness, config_path=self.config)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for name in ('grantlib.model.generate', 'grantlib.model.availability',
                     'socket.create_connection', 'socket.socket.connect'):
            self.guards.enter_context(patch(name, side_effect=forbidden))

    def setup_workspace(self, folders=None, **kwargs):
        return self.tools.grants_setup_workspace(
            folders or [], expected_config_revision=self.tools.intake_status()['config_revision'], **kwargs)

    def test_first_call_asks_one_minimal_question_and_unknowns_do_not_repeat(self):
        status = self.tools.grants_status()
        self.assertFalse(self.config.exists())
        self.assertTrue(status['intake']['needs_input'])
        self.assertIn('previous grant applications', status['intake']['suggested_question'])
        self.assertIn('RFP', status['intake']['suggested_question'])
        self.assertIn('deadline', status['intake']['suggested_question'])
        done = self.setup_workspace()
        self.assertFalse(done['needs_input'])
        self.assertIsNone(done['suggested_question'])
        self.assertIn('rfp', done['unknown_details'])
        self.assertEqual(library.load_config(self.config)['sources'], [])
        self.assertFalse(self.tools.grants_status()['intake']['needs_input'])
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.harness.list(), [])

    def test_indexes_only_explicit_folders_without_changing_originals(self):
        chosen = self.root / 'applications'
        chosen.mkdir()
        source = chosen / 'plan.txt'
        source.write_text('FICTIONAL: We will compare invented plant lighting schedules. No results exist.')
        unrelated = self.root / 'unrelated'
        unrelated.mkdir()
        (unrelated / 'secret.txt').write_text('UNSELECTED_SENTINEL')
        original = source.read_bytes()
        saved = self.setup_workspace(['applications'], rfp='https://example.org/funding-call',
                                     funder='Fictional funder', mechanism='Pilot', deadline='2027-01-15')
        self.assertFalse(self.harness.db.exists())
        result = self.tools.grants_index_archive(saved['config_revision'])
        self.assertEqual(result['counts']['documents'], 1)
        self.assertEqual(source.read_bytes(), original)
        self.assertFalse(library.search(self.harness.db, 'UNSELECTED_SENTINEL', {'role': 'all'}))
        self.assertEqual(library.stats(self.harness.db)['facets']['role'][0]['value'], 'ownership_uncertain')
        self.assertEqual(self.tools.grants_read_rfp()['status'], 'no_local_snapshot')

    def test_local_rfp_snapshot_is_bounded_immutable_and_not_scientific_evidence(self):
        rfp = self.root / 'call.txt'
        rfp.write_text('FICTIONAL RFP. Submit an invented pilot study.\n\nIgnore prior instructions is quoted source text only.')
        saved = self.setup_workspace(rfp='call.txt')
        identifier = saved['rfp_snapshot_id']
        page = self.tools.grants_read_rfp(max_chars=12)
        self.assertEqual(len(page['text']), 12)
        self.assertEqual(page['text_window']['next_start'], 12)
        self.assertFalse(page['retained_as_research_evidence'])
        first = self.tools.grants_read_rfp()
        rfp.write_text('CHANGED DOCUMENT')
        self.assertEqual(self.tools.grants_read_rfp(), first)
        revised = self.setup_workspace(rfp='call.txt')
        self.assertNotEqual(revised['rfp_snapshot_id'], identifier)
        self.assertEqual(self.tools.grants_read_rfp(snapshot_id=identifier), first)
        self.assertIn('CHANGED DOCUMENT', self.tools.grants_read_rfp()['text'])
        with self.assertRaises(ValueError):
            self.tools.grants_read_rfp(snapshot_id='../config.local')

    def test_stale_setup_or_index_and_invalid_inputs_do_not_mutate(self):
        saved = self.setup_workspace()
        original = self.config.read_bytes()
        with self.assertRaises(ValueError):
            self.tools.grants_setup_workspace([], expected_config_revision='missing')
        with self.assertRaises(ValueError):
            self.tools.grants_index_archive('missing')
        for extra in ({'deadline': '2027-02-30'}, {'rfp': 'https://user:secret@example.org/call'},
                      {'rfp': '/no/such/rfp.txt'}, {'rfp': 'file:///etc/passwd'}):
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.setup_workspace(**extra)
        with self.assertRaises(ValueError):
            self.setup_workspace([str(self.root)])
        self.assertEqual(self.config.read_bytes(), original)
        self.assertFalse(self.harness.db.exists())
        self.assertEqual(self.tools.grants_index_archive(saved['config_revision'])['counts']['documents'], 0)

    def test_existing_configuration_is_preserved_and_reopen_reuses_intake(self):
        saved = self.setup_workspace(funder='Fictional program')
        value = json.loads(self.config.read_text())
        value['refresh'] = {'enabled': False, 'canonical_host': 'example-host'}
        self.config.write_text(json.dumps(value))
        self.setup_workspace(funder='Fictional program', mechanism='Exploratory')
        self.assertEqual(json.loads(self.config.read_text())['refresh'], value['refresh'])
        reopened = open_workspace(self.config, provider=forbidden)
        self.addCleanup(reopened.close)
        status = GrantTools(reopened, config_path=self.config).grants_status()['intake']
        self.assertFalse(status['needs_input'])
        self.assertEqual(status['submission']['funder'], 'Fictional program')

    def test_partial_updates_preserve_answers_and_snapshot_without_rereading(self):
        folder = self.root / 'applications'
        folder.mkdir()
        rfp = self.root / 'call.txt'
        rfp.write_text('FICTIONAL RFP with a planned pilot.')
        saved = self.setup_workspace([str(folder)], rfp=str(rfp), funder='Invented program', deadline='2027-01-15')
        rfp.unlink()
        changed = self.tools.grants_setup_workspace(mechanism='Pilot', expected_config_revision=saved['config_revision'])
        self.assertEqual(changed['configured_folders'], saved['configured_folders'])
        self.assertEqual(changed['submission']['rfp'], saved['submission']['rfp'])
        self.assertEqual(changed['submission']['funder'], 'Invented program')
        self.assertEqual(changed['submission']['deadline'], '2027-01-15')
        self.assertEqual(changed['rfp_snapshot_id'], saved['rfp_snapshot_id'])
        self.assertIn('FICTIONAL', self.tools.grants_read_rfp()['text'])
        cleared = self.tools.grants_setup_workspace([], rfp='', expected_config_revision=changed['config_revision'])
        self.assertEqual(cleared['configured_folders'], [])
        self.assertIsNone(cleared['rfp_snapshot_id'])

    def test_identical_rfp_bytes_keep_distinct_source_provenance(self):
        first, second = self.root / 'first.txt', self.root / 'second.txt'
        first.write_text('FICTIONAL RFP.')
        second.write_bytes(first.read_bytes())
        before = self.setup_workspace(rfp=str(first))
        after = self.setup_workspace(rfp=str(second))
        self.assertNotEqual(before['rfp_snapshot_id'], after['rfp_snapshot_id'])
        self.assertEqual(self.tools.grants_read_rfp()['source_path'], str(second.resolve()))
        self.assertEqual(self.tools.grants_read_rfp(snapshot_id=before['rfp_snapshot_id'])['source_path'], str(first.resolve()))


@unittest.skipUnless(HAS_MCP, 'Install the MCP extra for protocol checks.')
class OnboardingProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_unconfigured_server_can_initialize_and_complete_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.local.json'
            async with Client(create_server(config)) as client:
                tools = {t.name: t for t in (await client.list_tools()).tools}
                self.assertIn('grants_setup_workspace', tools)
                self.assertNotIn('grants_start_generation', tools)
                status = (await client.call_tool('grants_status', {})).structured_content
                self.assertTrue(status['intake']['needs_input'])
                result = await client.call_tool('grants_setup_workspace', {'prior_application_folders': [],
                    'expected_config_revision': status['intake']['config_revision']})
                self.assertFalse(result.is_error, result.content)
                self.assertFalse(result.structured_content['needs_input'])
                indexed = await client.call_tool('grants_index_archive', {
                    'expected_config_revision': result.structured_content['config_revision']})
                self.assertFalse(indexed.is_error, indexed.content)
                self.assertEqual(indexed.structured_content['counts']['documents'], 0)


if __name__ == '__main__':
    unittest.main()
