"""MCP research-loop integration with invented sources and forbidden inference."""
import asyncio
import base64
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from grantlib.harness import GrantHarness
from grantlib.mcp_server import GrantTools

HAS_MCP = importlib.util.find_spec('mcp') is not None
if HAS_MCP:
    from mcp import Client
    from grantlib.mcp_server import create_server


def forbidden(*args, **kwargs):
    raise AssertionError('Synthetic MCP research tests cannot call a model or public network.')


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def reference():
    return {'status': 'resolved', 'title': 'Fictional reference',
            'abstract': 'This invented study is exploratory. No efficacy is established. ' * 250,
            'canonical': {'pmid': '123456', 'pmcid': 'PMC123456'},
            'provider': 'synthetic', 'source_url': 'https://example.org/fictional-paper',
            'retrieved_at': '2026-01-01T00:00:00+00:00',
            'limitations': ['Invented metadata only.']}


def article():
    xml = '<article><body><p>Invented source.</p></body></article>'
    values = [('Methods', 'The invented experiment assigned trays independently.'),
              ('Results', 'The invented measurements were exploratory. Uncertainty remains.'),
              ('Discussion', 'UNSELECTED_SOURCE_CANARY. No general efficacy is established. ' * 300)]
    return {'status': 'retrieved', 'requested_pmcid': 'PMC123456',
            'canonical': {'pmcid': 'PMC123456', 'pmid': '123456'},
            'title': 'Entirely invented public article', 'article_version': 'PMC123456.1',
            'source_url': 'https://example.org/fictional-paper',
            'provider': 'synthetic', 'retrieved_at': '2026-01-01T00:00:00+00:00',
            'license': {'code': 'CC0', 'open_access': True},
            'provenance': {'xml_sha256': digest(xml)},
            'paragraphs': [{'id': f'p{i:04d}', 'kind': 'body', 'section': section,
                            'text': text, 'text_sha256': digest(text),
                            'locator': f'{section}, paragraph {i}', 'xml_path': f'/article/body/p[{i}]'}
                           for i, (section, text) in enumerate(values, 1)],
            'raw_xml_b64': base64.b64encode(xml.encode()).decode()}


@unittest.skipUnless(HAS_MCP, 'Install the MCP extra for protocol tests.')
class MCPResearchWorkflowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.harness = GrantHarness(Path(self.tmp.name) / 'library.sqlite3', provider=forbidden,
                                    packet_builder=lambda *_: {'sources': [], 'coverage': {}, 'warnings': []})
        self.addCleanup(self.harness.close)
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for target in ('grantlib.model.generate', 'grantlib.model.availability',
                       'socket.create_connection', 'socket.socket.connect'):
            self.guards.enter_context(patch(target, side_effect=forbidden))

    async def call(self, client, name, **arguments):
        result = await client.call_tool(name, arguments)
        self.assertFalse(result.is_error, result.content)
        return result.structured_content

    async def error(self, client, name, **arguments):
        result = await client.call_tool(name, arguments)
        self.assertTrue(result.is_error, result)
        return '\n'.join(getattr(item, 'text', '') for item in result.content)

    async def prepare(self, client):
        proposal = await self.call(client, 'grants_create_proposal', brief={
            'title': 'Fictional research workflow', 'question': 'PRIVATE_FIXTURE_QUESTION_NOT_FOR_PUBLIC_LOOKUP'})
        job = await self.call(client, 'grants_gather_evidence', proposal_id=proposal['id'],
                              expected_revision=proposal['revision'])
        for _ in range(200):
            observed = await self.call(client, 'grants_get_job', job_id=job['id'])
            if observed['status'] == 'completed':
                return await self.call(client, 'grants_get_proposal', proposal_id=proposal['id'])
            self.assertIn(observed['status'], ('queued', 'running'))
            await asyncio.sleep(.01)
        self.fail('Evidence job did not terminate.')

    async def test_chat_can_resolve_read_retain_assess_revise_and_export_without_inference(self):
        async with Client(create_server(harness=self.harness)) as client:
            proposal = await self.prepare(client)
            pid = proposal['id']
            parent = await self.call(client, 'grants_save_draft', proposal_id=pid,
                expected_revision=proposal['revision'], markdown='A fictional experiment is proposed.')
            old_evidence = await self.call(client, 'grants_get_evidence', proposal_id=pid,
                                           artifact_id=parent['artifact_id'])
            with patch('grantlib.references.resolve', return_value=reference()) as resolve:
                resolved = await self.call(client, 'grants_resolve_reference', proposal_id=pid,
                                           expected_revision=parent['revision'], identifier='123456')
                resolve.assert_called_once_with('123456')
            ref = resolved['reference']
            self.assertTrue(ref['mcp_text_window']['truncated'])
            read = await self.call(client, 'grants_read_reference', proposal_id=pid,
                                   reference_id=ref['id'], start=12000)
            self.assertEqual(ref['abstract'] + read['reference']['abstract'], reference()['abstract'])
            self.assertFalse(ref['verification']['semantic_support'])
            listed = await self.call(client, 'grants_list_references', proposal_id=pid)
            self.assertEqual(listed['items'][0]['id'], ref['id'])
            with patch('grantlib.publication_text.fetch', return_value=article()) as fetch:
                fetched = await self.call(client, 'grants_fetch_full_text', proposal_id=pid,
                                          expected_revision=resolved['revision'], pmcid='PMC123456')
                fetch.assert_called_once_with('PMC123456')
            document_id = fetched['document']['id']
            self.assertNotIn('paragraphs', fetched['document'])
            self.assertNotIn('raw_xml_b64', json.dumps(fetched))
            self.assertFalse(fetched['retained_as_proposal_evidence'])
            docs = await self.call(client, 'grants_list_full_texts', proposal_id=pid)
            self.assertEqual(docs['items'][0]['id'], document_id)
            page = await self.call(client, 'grants_read_full_text', proposal_id=pid,
                                   document_id=document_id, offset=1, limit=1, max_chars=20)
            self.assertEqual(page['next_offset'], 2)
            paragraph = page['items'][0]
            continuation = await self.call(client, 'grants_read_publication_paragraph',
                proposal_id=pid, document_id=document_id, paragraph_id=paragraph['id'], start=20)
            self.assertEqual(paragraph['text'] + continuation['paragraph']['text'], article()['paragraphs'][1]['text'])
            retained = await self.call(client, 'grants_retain_publication_passage', proposal_id=pid,
                expected_revision=fetched['revision'], document_id=document_id, paragraph_id='p0002')
            self.assertEqual(retained['source']['metadata']['retained_by'], 'assistant')
            self.assertEqual(retained['source']['metadata']['scope'], 'full_text_excerpt')
            self.assertFalse(retained['scientifically_verified'])
            repeated = await self.call(client, 'grants_retain_publication_passage', proposal_id=pid,
                expected_revision=retained['revision'], document_id=document_id, paragraph_id='p0002')
            self.assertEqual(repeated['revision'], retained['revision'])
            quote = 'The invented measurements remain exploratory.'
            body = quote + ' [[' + retained['source']['id'] + ']]'
            saved = await self.call(client, 'grants_save_draft', proposal_id=pid,
                expected_revision=retained['revision'], markdown=body, base_artifact_id=parent['artifact_id'])
            added = await self.call(client, 'grants_add_claim', proposal_id=pid,
                expected_revision=saved['revision'], artifact_id=saved['artifact_id'], quote=quote)
            judgment = await self.call(client, 'grants_assess_claim', proposal_id=pid,
                expected_revision=added['revision'], artifact_id=saved['artifact_id'],
                claim_id=added['claim']['id'], verdict='supported', source_id=retained['source']['id'],
                excerpt=retained['source']['text'], locator=retained['source']['metadata']['locator'],
                note='Supports only the exploratory status stated in this retained paragraph.')
            self.assertEqual(judgment['assessment']['reviewer'], 'assistant')
            self.assertEqual(judgment['assessment']['scope'], 'full_text_excerpt')
            queue = await self.call(client, 'grants_get_review_queue', proposal_id=pid,
                                   artifact_id=saved['artifact_id'])
            self.assertEqual(queue['revision'], judgment['revision'])
            after_old = await self.call(client, 'grants_get_evidence', proposal_id=pid,
                                        artifact_id=parent['artifact_id'])
            self.assertEqual(after_old['items'], old_evidence['items'])
            evidence = await self.call(client, 'grants_get_evidence', proposal_id=pid,
                                      artifact_id=saved['artifact_id'])
            self.assertEqual([item['id'] for item in evidence['items']], [retained['source']['id']])
            self.assertNotIn('UNSELECTED_SOURCE_CANARY', json.dumps(evidence))
            export = await self.call(client, 'grants_export', proposal_id=pid,
                                    artifact_id=saved['artifact_id'], format='md')
            self.assertIn('Recorded claim judgments', Path(export['local_path']).read_text())

    async def test_mutation_conflicts_and_scope_errors_prevent_lookup_or_changes(self):
        async with Client(create_server(harness=self.harness)) as client:
            p = await self.prepare(client)
            decision = await self.call(client, 'grants_record_decision', proposal_id=p['id'],
                expected_revision=p['revision'], text='Use independently assigned trays; sample access remains unknown.')
            self.assertEqual(decision['recorded_by'], 'assistant')
            self.assertEqual(decision['decision']['recorded_by'], 'assistant')
            self.assertFalse(decision['investigator_identity_authenticated'])
            reread = await self.call(client, 'grants_get_proposal', proposal_id=p['id'])
            self.assertEqual(reread['decisions'][0]['recorded_by'], 'assistant')
            with patch('grantlib.references.resolve', side_effect=forbidden), patch('grantlib.publication_text.fetch', side_effect=forbidden):
                await self.error(client, 'grants_resolve_reference', proposal_id=p['id'],
                                 expected_revision=p['revision'], identifier='123456')
                await self.error(client, 'grants_fetch_full_text', proposal_id=p['id'],
                                 expected_revision=p['revision'], pmcid='PMC123456')
            # Invalid input must fail inside the real resolver before network access.
            await self.error(client, 'grants_resolve_reference', proposal_id=p['id'],
                             expected_revision=decision['revision'], identifier='https://localhost/private')
            await self.error(client, 'grants_fetch_full_text', proposal_id=p['id'],
                             expected_revision=decision['revision'], pmcid='/private/source.xml')
            with patch('grantlib.publication_text.fetch', return_value=article()):
                fetched = await self.call(client, 'grants_fetch_full_text', proposal_id=p['id'],
                    expected_revision=decision['revision'], pmcid='PMC123456')
            other = await self.call(client, 'grants_create_proposal', brief={'title': 'Other fictional proposal', 'question': 'Other question'})
            for name, extra in [('grants_read_full_text', {}), ('grants_read_publication_paragraph', {'paragraph_id': 'p0001'}),
                                ('grants_retain_publication_passage', {'paragraph_id': 'p0001', 'expected_revision': other['revision']})]:
                await self.error(client, name, proposal_id=other['id'], document_id=fetched['document']['id'], **extra)
            await self.error(client, 'grants_read_full_text', proposal_id=p['id'],
                             document_id=fetched['document']['id'], limit=1000)
            self.assertEqual((await self.call(client, 'grants_get_proposal', proposal_id=p['id']))['revision'], fetched['revision'])

    async def test_decision_citation_keeps_assistant_recorder_without_changing_old_draft(self):
        async with Client(create_server(harness=self.harness)) as client:
            p = await self.prepare(client)
            parent = await self.call(client, 'grants_save_draft', proposal_id=p['id'],
                                     expected_revision=p['revision'], markdown='This fictional study remains planned.')
            original = deepcopy(self.harness.artifact(p['id'], parent['artifact_id']))
            decision = await self.call(client, 'grants_record_decision', proposal_id=p['id'],
                expected_revision=parent['revision'], text='The investigator asks to retain a feasibility limit.')
            saved = await self.call(client, 'grants_save_draft', proposal_id=p['id'],
                expected_revision=decision['revision'], base_artifact_id=parent['artifact_id'],
                markdown='A feasibility constraint was recorded. [[' + decision['decision']['id'] + ']]')
            evidence = await self.call(client, 'grants_get_evidence', proposal_id=p['id'], artifact_id=saved['artifact_id'])
            self.assertEqual(evidence['items'][0]['metadata']['recorded_by'], 'assistant')
            self.assertFalse(evidence['items'][0]['metadata']['investigator_identity_authenticated'])
            self.assertEqual(evidence['items'][0]['metadata']['evidence_status'], 'recorded_decision')
            self.assertEqual(self.harness.artifact(p['id'], parent['artifact_id']), original)
            schema = next(tool for tool in (await client.list_tools()).tools if tool.name == 'grants_record_decision').input_schema
            self.assertNotIn('actor', schema['properties'])

    async def test_memory_can_be_proposed_but_not_confirmed_by_the_model(self):
        async with Client(create_server(harness=self.harness)) as client:
            p = await self.prepare(client)
            catalog = await self.call(client, 'grants_list_memory')
            data = {'category': 'constraint', 'text': 'Fictional equipment access remains unknown.',
                    'scope': 'Fictional proposal only', 'provenance_note': 'Assistant suggestion, not confirmed.'}
            for extra in ({'status': 'confirmed'}, {'status': 'retired'}, {'actor': 'investigator'}):
                await self.error(client, 'grants_propose_memory', expected_catalog_revision=catalog['catalog_revision'], data={**data, **extra})
            proposed = await self.call(client, 'grants_propose_memory',
                                      expected_catalog_revision=catalog['catalog_revision'], data=data)
            record = proposed['record']
            self.assertEqual(record['status'], 'proposed')
            self.assertEqual(record['actor'], 'assistant')
            selection = [{'entry_id': record['id'], 'version_id': record['version_id']}]
            await self.error(client, 'grants_select_memory', proposal_id=p['id'], expected_revision=p['revision'],
                             expected_catalog_revision=proposed['catalog_revision'], selections=selection)
            # Simulate an actual trusted investigator action outside MCP.
            confirmed = self.harness.store.revise_memory(record['id'], proposed['catalog_revision'],
                                                         {**data, 'status': 'confirmed'}, actor='investigator')
            actual = confirmed['entries'][0]
            chosen = await self.call(client, 'grants_select_memory', proposal_id=p['id'], expected_revision=p['revision'],
                expected_catalog_revision=confirmed['revision'], selections=[{'entry_id': actual['id'], 'version_id': actual['version_id']}])
            self.assertEqual(chosen['memory_bindings'][0]['current_state'], 'current')
            await self.error(client, 'grants_propose_memory', expected_catalog_revision=proposed['catalog_revision'], data=data)
            revised = await self.call(client, 'grants_propose_memory', expected_catalog_revision=confirmed['revision'],
                                      entry_id=actual['id'], data={**data, 'text': 'A new unconfirmed suggestion.'})
            self.assertEqual(revised['record']['status'], 'proposed')
            self.assertIsNone(revised['record']['confirmed_at'])
            self.assertEqual((await self.call(client, 'grants_get_proposal', proposal_id=p['id']))['memory_bindings'][0]['current_state'], 'proposed')
            names = {tool.name for tool in (await client.list_tools()).tools}
            self.assertFalse(any('confirm' in name for name in names))

    async def test_claim_reviews_cannot_forge_quotes_or_investigator_identity(self):
        async with Client(create_server(harness=self.harness)) as client:
            p = await self.prepare(client)
            saved = await self.call(client, 'grants_save_draft', proposal_id=p['id'],
                expected_revision=p['revision'], markdown='This fictional study remains proposed.')
            await self.error(client, 'grants_add_claim', proposal_id=p['id'],
                expected_revision=saved['revision'], artifact_id=saved['artifact_id'], quote='This study proved efficacy.')
            claim = await self.call(client, 'grants_add_claim', proposal_id=p['id'],
                expected_revision=saved['revision'], artifact_id=saved['artifact_id'], quote='This fictional study remains proposed.')
            await self.error(client, 'grants_assess_claim', proposal_id=p['id'], expected_revision=claim['revision'],
                artifact_id=saved['artifact_id'], claim_id=claim['claim']['id'], verdict='supported', note='No source was read.')
            recorded = await self.call(client, 'grants_assess_claim', proposal_id=p['id'], expected_revision=claim['revision'],
                artifact_id=saved['artifact_id'], claim_id=claim['claim']['id'], verdict='unresolved', note='No supporting source has been retained.')
            self.assertEqual(recorded['assessment']['reviewer'], 'assistant')
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            self.assertNotIn('reviewer', tools['grants_assess_claim'].input_schema['properties'])
            self.assertNotIn('actor', tools['grants_retain_publication_passage'].input_schema['properties'])
            self.assertTrue(tools['grants_resolve_reference'].annotations.open_world_hint)
            self.assertTrue(tools['grants_fetch_full_text'].annotations.open_world_hint)
            self.assertFalse(tools['grants_read_full_text'].annotations.open_world_hint)
            self.assertTrue(tools['grants_get_review_queue'].annotations.read_only_hint)


if __name__ == '__main__':
    unittest.main()
