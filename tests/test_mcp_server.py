"""MCP protocol and provenance checks with invented data and local transports.

The optional SDK tests skip when the MCP extra is absent. No real archive,
model, authentication service or network endpoint is used.
"""
import asyncio
from contextlib import ExitStack
import hashlib
import importlib.util
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from grantlib import library, nih
from grantlib.harness import GrantHarness
from tests.test_harness import FictionalProvider


HAS_MCP = importlib.util.find_spec('mcp') is not None
if HAS_MCP:
    import anyio
    from mcp import Client, StdioServerParameters
    from grantlib.mcp_server import create_server


REPO = Path(__file__).resolve().parents[1]
SOURCE_TEXT = (
    'FICTIONAL TEST DATA. We will compare two irrigation buffers in an invented seedling '
    'preservation experiment. This is proposed work; no protective effect has been '
    'demonstrated. Repeated sample readings are not independent seedlings. '
    + 'The planned fictional irrigation measurement remains conditional on access. ' * 110
)
BRIEF = {'title': 'Wholly fictional MCP proposal',
         'question': 'Which invented seedling irrigation buffer is informative?',
         'keywords': ['seedling', 'irrigation'], 'projects': ['InventedProject'],
         'notes': 'FICTIONAL TEST ONLY. No real organs, results or capabilities.'}


def forbidden(*_args, **_kwargs):
    raise AssertionError('MCP fixtures must not call a model, auth probe or network service.')


STDIO_GUARD = '''from contextlib import ExitStack
from unittest.mock import patch
from grantlib.mcp_server import main
def forbidden(*args, **kwargs):
    raise AssertionError('Unexpected model, authentication or network request in MCP fixture')
with ExitStack() as stack:
    for name in ('grantlib.model.generate', 'grantlib.model.availability',
                 'socket.create_connection', 'socket.socket.connect'):
        stack.enter_context(patch(name, side_effect=forbidden))
    main()
'''


@unittest.skipUnless(HAS_MCP, 'Install the optional MCP extra to run SDK integration tests.')
class MCPServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = self.root / 'invented-archive'
        self.source = self.archive / 'InventedProject' / 'Research_plan.txt'
        self.source.parent.mkdir(parents=True)
        self.source.write_text(SOURCE_TEXT)
        self.data = self.root / 'state'
        self.config = self.root / 'config.json'
        self.config_value = {'data_dir': str(self.data), 'workers': 1,
            'sources': [{'name': 'fictional', 'path': str(self.archive),
                         'default_role': 'ownership_uncertain', 'project_from_subfolder': True}]}
        self.config.write_text(json.dumps(self.config_value))
        library.ingest(self.config_value)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for name in ('grantlib.model.generate', 'grantlib.model.availability',
                     'socket.create_connection', 'socket.socket.connect'):
            self.guards.enter_context(patch(name, side_effect=forbidden))

    def harness(self, provider=forbidden):
        harness = GrantHarness(self.data / 'library.sqlite3', provider=provider)
        self.addCleanup(harness.close)
        return harness

    async def call(self, client, name, **arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        self.assertIsInstance(result.structured_content, dict)
        return result.structured_content

    async def error(self, client, name, **arguments):
        result = await client.call_tool(name, arguments)
        self.assertTrue(result.is_error, result)
        return '\n'.join(getattr(item, 'text', '') for item in result.content)

    async def terminal(self, client, job):
        for _ in range(200):
            observed = await self.call(client, 'grants_get_job', job_id=job['id'])
            self.assertEqual(observed['id'], job['id'])
            if observed['status'] not in ('queued', 'running'):
                return observed
            await asyncio.sleep(.01)
        self.fail('Synthetic local job did not terminate in the bounded observation window.')

    async def prepared(self, client):
        proposal = await self.call(client, 'grants_create_proposal', brief=BRIEF)
        job = await self.call(client, 'grants_gather_evidence', proposal_id=proposal['id'],
                              expected_revision=proposal['revision'])
        result = await self.terminal(client, job)
        self.assertEqual(result['status'], 'completed', result)
        self.assertIsNone(result['result_artifact_id'])
        proposal = await self.call(client, 'grants_get_proposal', proposal_id=proposal['id'])
        evidence = await self.call(client, 'grants_get_evidence', proposal_id=proposal['id'])
        source = next(item for item in evidence['items'] if item['layer'] == 'personal')
        return proposal, source

    async def test_discovery_schema_and_default_route_do_not_expose_generation(self):
        harness = self.harness()
        server = create_server(self.config, harness=harness)
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertGreaterEqual(len(tools), 34)
            self.assertNotIn('grants_start_generation', tools)
            self.assertTrue(tools['grants_search_archive'].annotations.read_only_hint)
            self.assertFalse(tools['grants_save_draft'].annotations.read_only_hint)
            self.assertTrue(tools['grants_verify_official_source'].annotations.open_world_hint)
            self.assertFalse(tools['grants_read_official_snapshot'].annotations.open_world_hint)
            save = tools['grants_save_draft'].input_schema
            self.assertIn('expected_revision', save['required'])
            self.assertNotIn('author', save['properties'])
            self.assertNotIn('output_path', tools['grants_export'].input_schema['properties'])
            status = await self.call(client, 'grants_status')
            self.assertFalse(status['generation_enabled'])
            self.assertEqual(status['archive_counts']['documents'], 1)
            self.assertEqual(status['proposal_count'], 0)
            await self.error(client, 'grants_start_generation', action='design')
        self.assertTrue(harness._closed)

    async def test_freshness_publication_discovery_and_explicit_live_page(self):
        harness = self.harness()
        from tests.test_guidance_refresh import page, URL
        async with Client(create_server(self.config, harness=harness)) as client:
            status = await self.call(client, 'grants_status')
            self.assertTrue(status['freshness']['layers']['publications']['stale'])
            found = await self.call(client, 'grants_search_publications', query='invented')
            self.assertEqual(found['results'], [])
            guidance = await self.call(client, 'grants_search_guidance', query='writing')
            self.assertIn('page_checks', guidance)
            with patch('grantlib.guidance_refresh._http_request', return_value=page('Invented official page text.')) as fetch:
                live = await self.call(client, 'grants_verify_official_source', url=URL, max_chars=8)
                self.assertFalse(live['requirements_verified'])
                more = await self.call(client, 'grants_read_official_snapshot',
                                       snapshot_id=live['source_snapshot_id'], start=8)
                self.assertIn('Invented official page text.', live['text'] + more['text'])
                self.assertEqual(fetch.call_count, 1)
            await self.error(client, 'grants_verify_official_source', url='http://localhost/')
            with patch('grantlib.guidance_refresh._http_request', side_effect=OSError('HTTP 403')):
                error = await self.error(client, 'grants_verify_official_source', url=URL)
                self.assertIn('Official source live retrieval failed', error)
                self.assertNotIn('Local grant storage is unavailable', error)

    async def test_search_windows_preserve_source_role_negation_and_exact_text(self):
        harness = self.harness()
        async with Client(create_server(self.config, harness=harness)) as client:
            search = await self.call(client, 'grants_search_archive', query='protective')
            self.assertEqual(search['role_scope'], 'all')
            self.assertFalse(search['retained_as_proposal_evidence'])
            row = search['results'][0]
            self.assertEqual(row['role'], 'ownership_uncertain')
            self.assertIn('no protective effect', row['text'])
            self.assertEqual(row['source_uri'], self.source.as_uri())
            first = await self.call(client, 'grants_read_archive_passage',
                                    unit_id='P_' + row['id'], start=0, max_chars=100)
            second = await self.call(client, 'grants_read_archive_passage',
                                     unit_id=row['id'], start=100, max_chars=10000)
            full = library.search(harness.db, 'protective', {'role': 'all'}, 1)[0]['text']
            self.assertEqual(first['text'] + second['text'], full)
            self.assertTrue(first['mcp_text_window']['truncated'])
            self.assertEqual(first['mcp_text_window']['next_start'], 100)
            self.assertIsNone(second['mcp_text_window']['next_start'])
            self.assertEqual(first['mcp_text_window']['full_text_sha256'],
                             hashlib.sha256(full.encode()).hexdigest())
            own = await self.call(client, 'grants_search_archive', query='irrigation', role='own_grant')
            self.assertEqual(own['results'], [])
            await self.error(client, 'grants_read_archive_passage', unit_id=str(self.source))
            await self.error(client, 'grants_read_archive_passage', unit_id=row['id'], start=len(full) + 1)
            await self.error(client, 'grants_search_archive', query='irrigation', limit=999)
            self.assertEqual((await self.call(client, 'grants_list_proposals'))['items'], [])

    async def test_local_evidence_save_revision_and_exports_preserve_source_snapshot(self):
        harness = self.harness()
        async with Client(create_server(self.config, harness=harness)) as client:
            proposal, source = await self.prepared(client)
            body = 'This fictional experiment remains proposed, with no demonstrated protection. [[' + source['id'] + ']]'
            saved = await self.call(client, 'grants_save_draft', proposal_id=proposal['id'],
                                    expected_revision=proposal['revision'], markdown=body)
            self.assertEqual(saved['origin'], 'assistant_import')
            self.assertEqual(saved['scientific_check'], 'not_run')
            self.assertEqual(saved['citation_check']['unknown_ids'], [])
            parent = await self.call(client, 'grants_get_artifact', proposal_id=proposal['id'],
                                     artifact_id=saved['artifact_id'])
            self.assertEqual(parent['markdown'], body)
            self.assertEqual(parent['scientific_check']['status'], 'not_run')
            self.assertNotIn('model_metadata', parent)
            retained = await self.call(client, 'grants_get_evidence', proposal_id=proposal['id'],
                                       artifact_id=saved['artifact_id'])
            self.assertEqual(next(item for item in retained['items'] if item['id'] == source['id']), source)
            child = await self.call(client, 'grants_save_draft', proposal_id=proposal['id'],
                                    expected_revision=saved['revision'], markdown=body + ' Precision is unknown.',
                                    base_artifact_id=saved['artifact_id'])
            self.assertEqual(child['base_artifact_id'], saved['artifact_id'])
            reread = await self.call(client, 'grants_get_artifact', proposal_id=proposal['id'],
                                     artifact_id=saved['artifact_id'])
            self.assertEqual(reread, parent)
            for format in ('md', 'docx'):
                exported = await self.call(client, 'grants_export', proposal_id=proposal['id'],
                                           artifact_id=child['artifact_id'], format=format)
                path = Path(exported['local_path'])
                self.assertEqual(path.parent, self.data.resolve() / 'mcp-exports')
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(exported['bytes'], path.stat().st_size)
                self.assertEqual(exported['sha256'], hashlib.sha256(path.read_bytes()).hexdigest())
                if format == 'md':
                    self.assertEqual(path.read_text(), harness.export(proposal['id'], child['artifact_id']))
                    self.assertIn('Precision is unknown.', path.read_text())
                    self.assertIn(source['source_uri'], path.read_text())
                    self.assertIn('ownership_uncertain', path.read_text())
                else:
                    with zipfile.ZipFile(path) as archive:
                        xml = archive.read('word/document.xml').decode()
                    self.assertIn('Precision is unknown.', xml)
                    self.assertIn('ownership_uncertain', xml)

    async def test_conflicts_branch_selection_and_unknown_sources_save_nothing(self):
        harness = self.harness()
        async with Client(create_server(self.config, harness=harness)) as client:
            proposal, source = await self.prepared(client)
            pid, revision = proposal['id'], proposal['revision']
            await self.error(client, 'grants_save_draft', proposal_id=pid,
                             expected_revision=revision, markdown='Invented claim. [[P_missing]]')
            unchanged = await self.call(client, 'grants_get_proposal', proposal_id=pid)
            self.assertEqual(unchanged['revision'], revision)
            self.assertEqual(unchanged['artifacts'], [])
            saved = await self.call(client, 'grants_save_draft', proposal_id=pid,
                                    expected_revision=revision, markdown='Proposed. [[' + source['id'] + ']]')
            await self.error(client, 'grants_save_draft', proposal_id=pid,
                             expected_revision=saved['revision'], markdown='An ambiguous branch.')
            await self.error(client, 'grants_save_draft', proposal_id=pid,
                             expected_revision=revision, markdown='A stale edit.',
                             base_artifact_id=saved['artifact_id'])
            await self.error(client, 'grants_update_brief', proposal_id=pid,
                             expected_revision=revision, brief={**BRIEF, 'title': 'Stale replacement'})
            after = await self.call(client, 'grants_get_proposal', proposal_id=pid)
            self.assertEqual(after['revision'], saved['revision'])
            self.assertEqual(len(after['artifacts']), 1)
            self.assertEqual(after['brief']['title'], BRIEF['title'])

    async def test_public_project_and_guidance_tools_remain_local_and_source_labeled(self):
        from tests.test_nih import record
        harness = self.harness()
        async with Client(create_server(self.config, harness=harness)) as client:
            missing = await self.call(client, 'grants_search_funded_projects', query='seedling')
            self.assertEqual(missing['results'], [])
            self.assertFalse((self.data / 'nih.sqlite3').exists())
            abstract = 'FICTIONAL: we will test seedling irrigation. ' + 'This invented planned study has no result. ' * 180
            connection = nih.initialize(self.data / 'nih.sqlite3')
            try:
                nih.import_api_record(connection, record(abstract=abstract))
                connection.commit()
            finally:
                connection.close()
            projects = await self.call(client, 'grants_search_funded_projects', query='seedling',
                                       fiscal_year=2025, activity='R01')
            self.assertEqual(projects['coverage'], 'imported_records_only')
            self.assertEqual(projects['evidence_label'], 'funded_project_description')
            self.assertEqual(projects['results'][0]['abstract'], abstract.strip())
            self.assertGreater(len(projects['results'][0]['abstract']), 6000)
            advice = await self.call(client, 'grants_search_guidance', mechanism='R01', limit=1)
            self.assertEqual(len(advice['items']), 1)
            self.assertIn('checked_on', advice['items'][0])
            self.assertTrue(advice['items'][0]['source_url'].startswith('https://'))
            self.assertTrue(advice['items'][0]['caveats'])
            self.assertEqual(advice['next_offset'], 1)
            continuation = await self.call(client, 'grants_search_guidance', mechanism='R01', limit=1,
                                           offset=advice['next_offset'])
            self.assertEqual(continuation['offset'], 1)
            self.assertNotEqual(continuation['items'][0]['id'], advice['items'][0]['id'])

    async def test_evidence_jobs_remain_cancellable_without_model_execution(self):
        entered, release = threading.Event(), threading.Event()
        def packet_builder(db, brief):
            entered.set()
            if not release.wait(4):
                raise AssertionError('Synthetic evidence builder was not released')
            return {'sources': [], 'coverage': {}, 'warnings': []}
        harness = GrantHarness(self.data / 'library.sqlite3', provider=forbidden,
                               packet_builder=packet_builder)
        self.addCleanup(harness.close)
        self.addCleanup(release.set)
        async with Client(create_server(self.config, harness=harness)) as client:
            proposal = await self.call(client, 'grants_create_proposal', brief=BRIEF)
            job = await self.call(client, 'grants_gather_evidence', proposal_id=proposal['id'],
                                  expected_revision=proposal['revision'])
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set())
            self.assertFalse((await self.call(client, 'grants_status'))['generation_enabled'])
            await self.call(client, 'grants_cancel_job', job_id=job['id'])
            release.set()
            result = await self.terminal(client, job)
            self.assertEqual(result['status'], 'cancelled')
            self.assertIsNone(result['result_artifact_id'])
        self.assertTrue(harness._closed)

    async def test_server_always_forbids_inference_even_with_injected_provider(self):
        harness = self.harness()
        server = create_server(self.config, harness=harness)
        with self.assertRaisesRegex(RuntimeError, 'Model inference is unavailable'):
            harness.service.generate('A model request must never execute')
        with self.assertRaises(TypeError):
            create_server(self.config, enable_generation=True)
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertFalse(any('generation' in name for name in tools))
            await self.error(client, 'grants_start_generation', action='design')

    async def test_actual_candidate_is_available_before_automatic_opinions(self):
        class ExplainingProvider(FictionalProvider):
            def __call__(self, prompt, **options):
                response = super().__call__(prompt, **options)
                if 'markdown' in response['structured']:
                    response['structured']['change_summary'] = 'An attributed writer explanation.'
                return response
        # This fixture uses its own invented packet, matching its citations.
        from tests.test_harness import packet
        harness = GrantHarness(self.data / 'library.sqlite3', provider=ExplainingProvider(),
                               packet_builder=packet)
        self.addCleanup(harness.close)
        # Historical artifacts can have assessments; the MCP server only reads them.
        proposal = harness.create(BRIEF)
        job = harness.run(proposal['id'], 'design', expected_revision=proposal['revision'])
        self.assertEqual(job['status'], 'completed', job)
        async with Client(create_server(self.config, harness=harness)) as client:
            args = {'proposal_id': proposal['id'], 'artifact_id': job['result_artifact_id']}
            plain = await self.call(client, 'grants_get_artifact', **args)
            assessed = await self.call(client, 'grants_get_artifact', **args, include_assessments=True)
            self.assertEqual(plain['markdown'], assessed['markdown'])
            self.assertEqual(plain['claims'], assessed['claims'])
            self.assertNotIn('model_metadata', plain)
            self.assertNotIn('change_summary', plain['structured'])
            self.assertNotIn('warnings', plain['citation_check'])
            self.assertEqual(assessed['structured']['change_summary'], 'An attributed writer explanation.')
            self.assertIn('warnings', assessed['citation_check'])
            self.assertEqual(assessed['model_metadata']['draft_check']['status'], 'completed')
            self.assertEqual(assessed['model_metadata']['draft_check']['report']['state'], 'no_issues_reported')

    async def stdio_case(self, mode):
        # This child runs the real module entrypoint with explicit local guards.
        # A malformed stdout banner would break the SDK handshake/calls.
        params = StdioServerParameters(command=sys.executable,
            args=['-c', STDIO_GUARD, '--config', str(self.config)], cwd=str(REPO))
        async with Client(params, read_timeout_seconds=10, mode=mode) as client:
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertNotIn('grants_start_generation', names)
            self.assertIn('grants_get_review_queue', names)
            self.assertIn('grants_assess_claim', names)
            status = await self.call(client, 'grants_status')
            self.assertEqual(status['archive_counts']['documents'], 1)
            proposal, source = await self.prepared(client)
            body = 'This fictional protocol is proposed. [[' + source['id'] + ']]'
            saved = await self.call(client, 'grants_save_draft', proposal_id=proposal['id'],
                                    expected_revision=proposal['revision'], markdown=body)
            artifact = await self.call(client, 'grants_get_artifact', proposal_id=proposal['id'],
                                       artifact_id=saved['artifact_id'])
            self.assertEqual(artifact['markdown'], body)
            self.assertEqual(artifact['origin'], 'assistant_import')
            await self.error(client, 'grants_search_archive', query='irrigation', limit=-1)
            exported = await self.call(client, 'grants_export', proposal_id=proposal['id'],
                                       artifact_id=saved['artifact_id'], format='md')
            self.assertIn('This fictional protocol is proposed. [1]', Path(exported['local_path']).read_text())
            final = await self.call(client, 'grants_get_proposal', proposal_id=proposal['id'])
            self.assertEqual(len(final['artifacts']), 1)
            self.assertTrue(all(job['status'] == 'completed' for job in final['jobs']))

    async def test_reference_library_find_format_and_styled_export_with_companion_ris(self):
        from grantlib import publication_search
        from tests.test_publication_search import FakeTransport, esearch, icite
        from tests.test_references import article, pubmed
        harness = self.harness()
        async with Client(create_server(self.config, harness=harness)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertTrue(tools['grants_find_references'].annotations.open_world_hint)
            self.assertTrue(tools['grants_find_references'].annotations.read_only_hint)
            self.assertEqual(tools['grants_export'].input_schema['properties']['style']['enum'][0], 'vancouver')
            self.assertEqual(tools['grants_export'].input_schema['properties']['format']['enum'], ['md', 'docx', 'ris', 'csl'])
            proposal, source = await self.prepared(client)
            transport = FakeTransport({'esearch': esearch(['12345678']), 'efetch': pubmed(article()),
                                       'icite': icite([{'pmid': 12345678, 'citation_count': 5, 'relative_citation_ratio': 1.2}])})
            with patch.object(publication_search, '_network', transport):
                found = await self.call(client, 'grants_find_references', query='fictional seedling irrigation', limit=1, abstract_chars=10)
            self.assertEqual(found['candidates'][0]['pmid'], '12345678')
            self.assertEqual(found['candidates'][0]['citation_count'], 5)
            self.assertEqual(found['candidates'][0]['abstract'], 'METHODS: F')
            self.assertTrue(found['candidates'][0]['abstract_truncated'])
            self.assertIn('grants_resolve_reference', found['next_step'])
            self.assertIn('public topic words', await self.error(client, 'grants_find_references', query='see [[P_1]] PRIVATE'))
            self.assertFalse(any('PRIVATE' in url for url in transport.calls))
            calls = []
            def resolver(url, **kwargs):
                calls.append(url)
                return pubmed(article().replace('<JournalIssue>', '<JournalIssue><Volume>12</Volume>').replace(
                    '</AuthorList>', '</AuthorList><Pagination><MedlinePgn>100-110</MedlinePgn></Pagination>'))
            with patch('grantlib.references._network', resolver):
                resolved = await self.call(client, 'grants_resolve_reference', proposal_id=proposal['id'],
                                           expected_revision=proposal['revision'], identifier='12345678')
            self.assertEqual(len(calls), 1)
            reference = resolved['reference']
            self.assertEqual((reference['volume'], reference['pages']), ('12', '100-110'))
            library = await self.call(client, 'grants_format_references', proposal_id=proposal['id'], style='nature')
            self.assertEqual(library['count'], 1)
            self.assertEqual(library['entries'][0]['entry'], 'Author, E. & Invented Group. Fictional paired experiment. *Invented Journal* **12**, 100–110 (2024).')
            self.assertEqual([s['style'] for s in library['available_styles']][:2], ['vancouver', 'nature'])
            ris = await self.call(client, 'grants_format_references', proposal_id=proposal['id'], format='ris', reference_ids=[reference['id']])
            self.assertIn('TY  - JOUR\nAU  - Author, Example\n', ris['text'])
            await self.error(client, 'grants_format_references', proposal_id=proposal['id'], reference_ids=['R_missing'])
            await self.error(client, 'grants_format_references', proposal_id=proposal['id'], style='chicago')
            body = ('Local plan [[' + source['id'] + ']] and a published fictional experiment [[' + reference['id'] + ']].')
            saved = await self.call(client, 'grants_save_draft', proposal_id=proposal['id'],
                                    expected_revision=resolved['revision'], markdown=body)
            self.assertEqual(saved['citation_check']['unknown_ids'], [])
            exported = await self.call(client, 'grants_export', proposal_id=proposal['id'],
                                       artifact_id=saved['artifact_id'], format='md', style='cell')
            text = Path(exported['local_path']).read_text()
            self.assertIn('Local plan [1] and a published fictional experiment (Author and Invented Group, 2024).', text)
            self.assertIn('## References', text)
            self.assertIn('Author, E., and Invented Group (2024). Fictional paired experiment. *Invented Journal* *12*, 100–110.', text)
            self.assertLess(text.index('## References'), text.index('Source notes for investigator review'))
            companion = Path(exported['companion_ris']['local_path'])
            self.assertEqual(companion.parent, Path(exported['local_path']).parent)
            self.assertEqual(stat.S_IMODE(companion.stat().st_mode), 0o600)
            self.assertEqual(exported['companion_ris']['sha256'], hashlib.sha256(companion.read_bytes()).hexdigest())
            self.assertIn('AN  - 12345678', companion.read_text())
            self.assertNotIn(source['id'], companion.read_text())
            csl = await self.call(client, 'grants_export', proposal_id=proposal['id'], artifact_id=saved['artifact_id'], format='csl')
            items = json.loads(Path(csl['local_path']).read_text())
            self.assertEqual([item['id'] for item in items], [reference['id']])
            self.assertNotIn('companion_ris', csl)
            docx = await self.call(client, 'grants_export', proposal_id=proposal['id'], artifact_id=saved['artifact_id'], format='docx', style='nature')
            with zipfile.ZipFile(docx['local_path']) as archive:
                xml = archive.read('word/document.xml').decode()
            self.assertIn('superscript', xml)
            self.assertIn('Author, E. &amp; Invented Group. Fictional paired experiment.', xml)
            plain = await self.call(client, 'grants_export', proposal_id=proposal['id'], artifact_id=saved['artifact_id'], format='md')
            self.assertIn('Style: Vancouver', Path(plain['local_path']).read_text())
            await self.error(client, 'grants_export', proposal_id=proposal['id'], artifact_id=saved['artifact_id'], style='chicago')

    async def test_shared_library_is_harvested_from_the_archive_and_adopted_by_proposals(self):
        from tests.test_reference_library import FakeNetwork
        self.source.write_text(SOURCE_TEXT + '\n\nREFERENCES\n\n1. Author E. A fictional paired study. Invent J. 2024;12:100-110. doi:10.9999/fictional\n')
        library.ingest(self.config_value)
        harness = self.harness()
        async with Client(create_server(self.config, harness=harness)) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertTrue(tools['grants_harvest_archive_references'].annotations.open_world_hint)
            self.assertFalse(tools['grants_harvest_archive_references'].annotations.read_only_hint)
            self.assertTrue(tools['grants_search_library'].annotations.read_only_hint)
            self.assertIn('lancet', tools['grants_export'].input_schema['properties']['style']['enum'])
            status = await self.call(client, 'grants_status')
            self.assertEqual(status['reference_library_count'], 0)
            network = FakeNetwork()
            with patch('grantlib.references._network', network):
                preview = await self.call(client, 'grants_harvest_archive_references', dry_run=True)
                self.assertEqual((preview['pending_identifiers'], network.calls), (1, []))
                harvested = await self.call(client, 'grants_harvest_archive_references', limit=5)
            self.assertEqual([a['identity_key'] for a in harvested['added']], ['doi:10.9999/fictional'])
            self.assertEqual(harvested['remaining'], 0)
            found = await self.call(client, 'grants_search_library', query='paired')
            entry = found['items'][0]
            self.assertEqual(entry['cited_in']['projects'], ['InventedProject'])
            self.assertEqual(entry['identity_basis'], 'explicit_identifier')
            proposal, source = await self.prepared(client)
            adopted = await self.call(client, 'grants_add_library_reference', proposal_id=proposal['id'],
                                      expected_revision=proposal['revision'], library_reference_id=entry['id'])
            self.assertTrue(adopted['reference_id'].startswith('R_'))
            self.assertFalse(adopted['already_present'])
            self.assertIn(adopted['reference_id'], adopted['reading_hint'])
            await self.error(client, 'grants_add_library_reference', proposal_id=proposal['id'],
                             expected_revision=proposal['revision'], library_reference_id=entry['id'])
            with patch('grantlib.references._network', network):
                checked = await self.call(client, 'grants_check_references', proposal_id=proposal['id'], force=True)
            self.assertEqual(checked['checked'][0]['status'], 'checked')
            self.assertEqual(checked['flagged'], [])
            self.assertTrue(tools['grants_check_references'].annotations.open_world_hint)
            body = 'A published fictional study [[' + adopted['reference_id'] + ']].'
            saved = await self.call(client, 'grants_save_draft', proposal_id=proposal['id'],
                                    expected_revision=adopted['revision'], markdown=body)
            exported = await self.call(client, 'grants_export', proposal_id=proposal['id'], artifact_id=saved['artifact_id'], format='md')
            self.assertEqual(exported['reference_check']['cited_publications'], 1)
            self.assertEqual((exported['reference_check']['flagged'], exported['reference_check']['unchecked_or_stale']), ([], 0))
            self.assertIn('no notice flags', exported['reference_check']['advice'])
            formatted = await self.call(client, 'grants_format_references', proposal_id='', scope='library', style='lancet')
            self.assertEqual(formatted['count'], 1)
            self.assertEqual(formatted['entries'][0]['entry'], 'Author E, Fictional Consortium. Fictional paired study. *Invented Journal* 2025; 12: 100–110.')
            self.assertEqual((await self.call(client, 'grants_status'))['reference_library_count'], 1)
            await self.error(client, 'grants_harvest_archive_references', limit=0)

    async def test_actual_stdio_auto_protocol_uses_only_synthetic_archive_and_survives_tool_errors(self):
        await self.stdio_case('auto')

    async def test_actual_stdio_legacy_initialization_is_compatible(self):
        await self.stdio_case('legacy')
