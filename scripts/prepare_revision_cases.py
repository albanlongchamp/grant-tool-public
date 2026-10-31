#!/usr/bin/env python3
"""Prepare held-out fictional cases; model execution requires --run-model."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time
import uuid

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'tests/fixtures/revision_heldout.json'


def cases():
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    if fixture.get('fictional') is not True or fixture.get('version') != 1:
        raise ValueError('Only the committed invented cases are supported.')
    result = {}
    for case in fixture['cases']:
        if case.get('fictional') is not True or case['id'] in result:
            raise ValueError('Each case must be fictional and uniquely identified.')
        for source in case['sources']:
            if (source['metadata'].get('fictional') is not True
                    or not source['source_uri'].startswith('urn:fictional-benchmark:')
                    or not source['text'].startswith('FICTIONAL BENCHMARK:')):
                raise ValueError('Refusing a non-fictional source.')
        result[case['id']] = case
    return result


def build_inputs(case):
    """Keep reviewer expectations out of both audit and revision input data."""
    parent = copy.deepcopy(case['parent'])
    parent.update(id=hashlib.sha256(case['id'].encode()).hexdigest()[:32], kind='aims',
                  recorded_claim_judgments=[])
    packet = {'version': 1, 'sources': copy.deepcopy(case['sources']),
              'coverage': {'synthetic_only': True, 'real_archive_opened': False},
              'warnings': ['Invented benchmark only; no actual scientific evidence or funder requirements.']}
    context = {'brief': copy.deepcopy(case['brief']),
               'investigator_decisions': copy.deepcopy(case['decisions']),
               'selected_direction': '', 'investigator_instruction': case['instruction'],
               'selected_version': parent, 'related_versions': [], 'evidence_packet': packet}
    return context, copy.deepcopy(case['candidate'])


def prepare_case(output_dir, case_id):
    case = cases()[case_id]
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from grantlib import revision_check
    from scripts.evaluate_assistant import prompt_data, write_json, write_text, stamp, QUALITY_SCOPE

    context, candidate = build_inputs(case)
    prompt = revision_check.make_prompt(context, candidate)
    prompt_data(prompt)  # Assert the same fictional-source boundary as live harnesses.
    output_dir = Path(output_dir).expanduser().absolute()
    output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    output_dir = output_dir.resolve()
    write_json(output_dir / 'audit-input.json', {'context': context, 'candidate': candidate})
    write_text(output_dir / 'audit-input.txt', prompt)
    write_json(output_dir / 'audit-schema.json', revision_check.SCHEMA)
    write_json(output_dir / 'revision-seed.json', {
        'brief': context['brief'], 'decisions': context['investigator_decisions'],
        'packet': context['evidence_packet'], 'parent': case['parent'],
        'instruction': case['instruction'],
    })
    write_text(output_dir / 'parent.md', case['parent']['markdown'])
    write_text(output_dir / 'candidate.md', candidate['markdown'])
    write_json(output_dir / 'reviewer-expectations.json', case['reviewer_expectations'])
    write_json(output_dir / 'summary.json', {
        'status': 'prepared', 'case_id': case_id, 'synthetic_only': True, 'model_calls': 0,
        'created_at': stamp(), 'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
        'quality_scope': QUALITY_SCOPE,
        'note': 'The candidate deliberately contains faults. Reviewer expectations are not model input. '
                'revision-seed.json supplies the clean parent for a separate Service revision evaluation.',
    })
    return output_dir


def run_case(output_dir, *, generate, mode='injected', timeout=900):
    """Audit the fixture candidate through the production bounded-repair path.

    The initial candidate is explicitly injected; only check, optional repair,
    and final check use the supplied generator. At most three calls are allowed.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive')
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from grantlib.revision_pipeline import check_revision
    from grantlib.studio import OUTPUT_SCHEMA, make_prompt, validate_output, export_markdown
    from grantlib.docx_export import export_docx
    from scripts.evaluate_assistant import prompt_data, write_json, write_text, stamp, word_counts

    output_dir = Path(output_dir).resolve()
    summary = json.loads((output_dir / 'summary.json').read_text())
    if summary['status'] != 'prepared' or (output_dir / 'calls').exists():
        raise ValueError('Use a freshly prepared case; evaluations are never resumed or overwritten.')
    context, candidate = build_inputs(cases()[summary['case_id']])
    payload = json.loads((output_dir / 'audit-input.json').read_text())
    if payload != {'context': context, 'candidate': candidate}:
        raise ValueError('Prepared inputs differ from the committed invented case.')
    prompt_data((output_dir / 'audit-input.txt').read_text())
    original_parent = copy.deepcopy(context['selected_version'])
    original_candidate = copy.deepcopy(candidate)
    summary.update(status='running', generation_mode=mode, calls=[], model_calls=0,
                   initial_candidate_origin='invented fixture, not a model-generated draft',
                   started_at=stamp())
    cancel = threading.Event()
    started = time.monotonic()
    deadline = started + timeout
    def persist():
        write_json(output_dir / 'summary.json', summary)
    def phase(message):
        event = {'at': stamp(), 'case': summary['case_id'], 'phase': message}
        with (output_dir / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(event) + '\n')
        print(json.dumps(event), flush=True)
    def recorded_generate(prompt, **kwargs):
        data = prompt_data(prompt)
        sent_context = data.get('context', data)
        if sent_context != context:
            raise ValueError('The model context differs from the committed invented case.')
        stages = ('check-initial', 'repair', 'check-final')
        stage = Path(kwargs['run_dir']).name
        index = len(summary['calls'])
        if index >= 3 or stage != stages[index]:
            raise ValueError('Only one check, one repair and one final check are permitted.')
        path = output_dir / 'calls' / f'{index + 1:02d}-{stage}'
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
        write_text(path / 'input.txt', prompt)
        write_json(path / 'input.json', data)
        write_json(path / 'schema.json', kwargs['schema'])
        call = {'index': index + 1, 'stage': stage, 'status': 'running',
                'input_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                'files': str(path.relative_to(output_dir)), 'started_at': stamp()}
        summary['calls'].append(call)
        summary['model_calls'] += 1
        persist()
        began = time.monotonic()
        try:
            result = generate(prompt, **kwargs)
            write_json(path / 'output.json', result)
            call.update({key: result.get(key) for key in ('status', 'provider', 'model', 'usage', 'run_id')})
            return result
        except BaseException as exc:
            call.update(status='failed', error=f'{type(exc).__name__}: {exc}'[:1600])
            raise
        finally:
            call['elapsed_seconds'] = round(time.monotonic() - began, 3)
            write_json(path / 'observation.json', call)
            persist()
    persist()
    try:
        initial = {'status': 'completed', 'structured': copy.deepcopy(candidate),
                   'provider': 'invented_fixture', 'model': None, 'usage': None, 'run_id': None}
        base_prompt = make_prompt({'brief': context['brief'], 'decisions': context['investigator_decisions']},
            context['evidence_packet'], 'revise',
            {'instruction': context['investigator_instruction'], 'selected_direction': ''},
            versions={'selected_version': context['selected_version'], 'related_versions': []})
        output, metadata = check_revision(initial, context=context, base_prompt=base_prompt,
            generate=recorded_generate, schema=OUTPUT_SCHEMA,
            validate_candidate=lambda value: validate_output(value, context['evidence_packet'], 'aims'),
            run_dir=output_dir / 'pipeline', on_event=lambda event: None, cancel_event=cancel,
            phase=phase, deadline=deadline)
        if context['selected_version'] != original_parent or candidate != original_candidate:
            raise ValueError('The original parent or fixture candidate was modified.')
        artifact = {'id': uuid.uuid4().hex, 'kind': 'aims', 'title': output['title'],
                    'markdown': output['markdown'], 'structured': output, 'claims': [],
                    'base_artifact_id': original_parent['id'], 'created_at': stamp(),
                    'context': context['evidence_packet'], 'model_metadata': metadata,
                    'citation_check': validate_output(output, context['evidence_packet'], 'aims'),
                    'origin': 'assistant' if metadata['revision_check']['repaired'] else 'invented_fixture'}
        proposal = {'brief': context['brief'], 'artifacts': [artifact]}
        write_json(output_dir / 'artifact.json', artifact)
        write_json(output_dir / 'pipeline-result.json', {'output': output, 'model_metadata': metadata})
        write_text(output_dir / 'revision.md', export_markdown(proposal, artifact['id']))
        (output_dir / 'revision.docx').write_bytes(export_docx(proposal, artifact['id']))
        summary.update(status='completed', original_parent_unchanged=True, original_candidate_unchanged=True,
                       artifact_id=artifact['id'], revision_check=metadata['revision_check'],
                       citation_check=artifact['citation_check'], word_counts=word_counts(output['markdown']))
    except BaseException as exc:
        cancel.set()
        summary.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                       error=f'{type(exc).__name__}: {exc}'[:1600])
        raise
    finally:
        summary.update(finished_at=stamp(), elapsed_seconds=round(time.monotonic() - started, 3))
        summary['total_usage'] = {}
        for call in summary['calls']:
            for key, value in (call.get('usage') or {}).items():
                if type(value) in (int, float):
                    summary['total_usage'][key] = summary['total_usage'].get(key, 0) + value
        persist()
        phase('Terminal: ' + summary['status'])
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=sorted(cases()))
    parser.add_argument('--output-dir', required=True, type=Path,
                        help='New private output directory under an existing parent.')
    parser.add_argument('--run-model', action='store_true',
                        help='Audit the invented candidate, with at most one repair and final check (1–3 model calls).')
    args = parser.parse_args(argv)
    try:
        path = prepare_case(args.output_dir, args.case)
        if args.run_model:
            from grantlib.model import generate
            run_case(path, generate=generate, mode='model')
            return 0
        print(json.dumps({'status': 'prepared', 'case_id': args.case, 'model_calls': 0, 'output_dir': str(path)}))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
