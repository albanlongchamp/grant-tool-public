#!/usr/bin/env python3
"""Exercise the MCP workflow with invented text and no model/network calls."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from mcp import Client
from grantlib.mcp_server import create_server


async def main():
    workspace = ROOT / '.local/demo-mcp'
    workspace.mkdir(parents=True, exist_ok=True)
    config = workspace / 'config.local.json'
    if not config.exists():
        config.write_text(json.dumps({'sources': [], 'archive_optional': True,
            'data_dir': 'state', 'workers': 1, 'refresh': {'enabled': False}}))
        config.chmod(0o600)
    async with Client(create_server(config)) as client:
        async def call(name, **arguments):
            result = await client.call_tool(name, arguments)
            if result.is_error:
                raise RuntimeError(str(result.content))
            return result.structured_content

        status = await call('grants_status')
        if status['intake']['needs_input']:
            print('First-use question: ' + status['intake']['suggested_question'])
            await call('grants_setup_workspace',
                prior_application_folders=[str(ROOT / 'examples/demo/sources')],
                rfp=str(ROOT / 'examples/demo/funding_call.txt'),
                funder='Fictional demonstration program', mechanism='Pilot',
                expected_config_revision=status['intake']['config_revision'])
            status = await call('grants_status')
        await call('grants_index_archive', expected_config_revision=status['intake']['config_revision'])
        rfp = await call('grants_read_rfp')
        if 'FICTIONAL' not in rfp.get('text', ''):
            raise RuntimeError('The demonstration requires its fictional RFP.')
        title = 'FICTIONAL MCP demo: lighting schedules and plant growth'
        proposals = await call('grants_list_proposals')
        proposal = next((p for p in proposals['items'] if p['title'] == title), None)
        if proposal is None:
            proposal = await call('grants_create_proposal', brief={
                'title': title,
                'question': 'How could lighting schedules affect growth in a fictional plant study?',
                'keywords': ['lighting', 'growth', 'trays'],
                'funder': 'Fictional demonstration program', 'mechanism': 'Pilot',
                'constraints': 'Invented demonstration only. No results or confirmed facilities.',
            })
        proposal = await call('grants_get_proposal', proposal_id=proposal['id'])
        if not proposal['artifacts']:
            job = await call('grants_gather_evidence', proposal_id=proposal['id'], expected_revision=proposal['revision'])
            for _ in range(200):
                job = await call('grants_get_job', job_id=job['id'])
                if job['status'] not in ('queued', 'running'):
                    break
                await asyncio.sleep(.01)
            if job['status'] != 'completed':
                raise RuntimeError('Demo evidence gathering did not complete.')
            proposal = await call('grants_get_proposal', proposal_id=proposal['id'])
            evidence = await call('grants_get_evidence', proposal_id=proposal['id'])
            source = next(s for s in evidence['items'] if s['layer'] == 'personal')
            markdown = (
                '# Fictional research aims\n\n'
                'This prewritten software demonstration is not a real grant or scientific finding.\n\n'
                'We propose to compare two lighting schedules using independently assigned growth trays. '
                'Repeated images within a tray are subsamples, not additional independent units. '
                f'The study has not been performed. [[{source["id"]}]]\n\n'
                '## Inputs to establish\n\n'
                'Confirm the growth measurement, equipment access, number of independent trays and achievable '
                'precision before finalizing the design. A wide interval around a null estimate would leave '
                f'the contrast unresolved. [[{source["id"]}]]\n'
            )
            await call('grants_save_draft', proposal_id=proposal['id'], expected_revision=proposal['revision'],
                       title='Prewritten fictional MCP demonstration', markdown=markdown)
            proposal = await call('grants_get_proposal', proposal_id=proposal['id'])
        artifact = proposal['artifacts'][0]
        queue = await call('grants_get_review_queue', proposal_id=proposal['id'], artifact_id=artifact['id'])
        exported = await call('grants_export', proposal_id=proposal['id'], artifact_id=artifact['id'], format='md')
    print('Fictional MCP demo ready. No model or network requests were made.')
    print('Review queue candidates:', queue['total'])
    print('Export:', Path(exported['local_path']).relative_to(ROOT))
    print('Connect your MCP client using .local/demo-mcp/config.local.json to continue.')


if __name__ == '__main__':
    asyncio.run(main())
