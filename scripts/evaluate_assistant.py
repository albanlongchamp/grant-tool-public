#!/usr/bin/env python3
"""Reproducible fictional Proposal Studio workflow; no model call by default."""
from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
import threading
import time

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / 'tests' / 'fixtures' / 'proposal_workflow.json'
QUALITY_SCOPE = ('Citation resolution checks identifiers and source metadata only. '
                 'It does not establish claim support or scientific quality. '
                 'Independent scientific review is required.')
REVISION_STAGES = ('draft', 'check-initial', 'repair', 'check-final')
CHECKED_ACTIONS = frozenset({'design', 'aims', 'strategy', 'revise'})
MIN_CALLS = 7
MAX_CALLS = 13
AUDIT_CONTRACTS = ('legacy', 'scientific_defects_v1')
AUDIT_PROTOCOLS = {
    'REVISION_SCIENTIFIC_AUDIT_V1': 'revision_scientific_audit',
    'DRAFT_SCIENTIFIC_AUDIT_V1': 'draft_scientific_audit',
    'REVISION_SCIENTIFIC_DEFECTS_V1_QUOTES': 'revision_scientific_defects_v1_quotes',
    'DRAFT_SCIENTIFIC_DEFECTS_V1_QUOTES': 'draft_scientific_defects_v1_quotes',
}
AUDIT_MARKERS = tuple(AUDIT_PROTOCOLS)


def audit_marker(action, contract):
    prefix = 'REVISION' if action == 'revise' else 'DRAFT'
    return prefix + ('_SCIENTIFIC_AUDIT_V1' if contract == 'legacy'
                     else '_SCIENTIFIC_DEFECTS_V1_QUOTES')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def write_text(path, text):
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def validate_fixture(fixture):
    if fixture.get('version') != 1 or fixture.get('fictional') is not True:
        raise ValueError('The benchmark must use the committed fictional fixture.')
    sources = fixture['initial_sources'] + [fixture['change']['source']]
    if len({s['id'] for s in sources}) != len(sources):
        raise ValueError('Fictional source IDs must be distinct.')
    for source in sources:
        if (source['metadata'].get('fictional') is not True
                or not source['source_uri'].startswith('urn:fictional-benchmark:')
                or not source['text'].startswith('FICTIONAL BENCHMARK:')
                or source['layer'] not in ('personal', 'public_project')):
            raise ValueError('The benchmark accepts invented sources only.')


def prepare(output_dir, *, audit_contract='legacy'):
    """Create a fresh private directory; never open an archive or model backend."""
    if audit_contract not in AUDIT_CONTRACTS:
        raise ValueError('Unknown evaluation audit contract.')
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    validate_fixture(fixture)
    output_dir = Path(output_dir).expanduser().absolute()
    # An existing directory, file or symlink is never reused or overwritten.
    output_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    output_dir = output_dir.resolve()
    write_json(output_dir / 'fixture.json', fixture)
    write_json(output_dir / 'summary.json', {
        'status': 'prepared', 'synthetic_only': True, 'model_calls': 0,
        'audit_contract': audit_contract,
        'output_dir': str(output_dir), 'created_at': stamp(),
        'fixture_sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        'prepared_fixture_sha256': hashlib.sha256((output_dir / 'fixture.json').read_bytes()).hexdigest(),
        'harness_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'quality_scope': QUALITY_SCOPE,
    })
    return output_dir


def word_counts(markdown, structured=None):
    """Use production measurements; old evaluation receipts remain unchanged."""
    from grantlib.draft_metrics import measure, METHOD
    measured = measure({'markdown': markdown, 'structured': structured or {}})
    return {**measured, 'main_draft_words': measured['words'], 'method': METHOD}


def prompt_data(prompt, *, allowed_decision_texts=None):
    """Decode known draft/audit envelopes without reading an external packet."""
    if prompt.startswith(AUDIT_MARKERS):
        marker = '\nAUDIT_INPUT_JSON:\n'
        payload = json.loads(prompt.split(marker, 1)[1])
        context = payload['context']
    else:
        marker = 'PROPOSAL AND SOURCE DATA (JSON):\n'
        raw = prompt.split(marker, 1)[1].lstrip()
        context, end = json.JSONDecoder().raw_decode(raw)
        payload = context
        tail = raw[end:].strip()
        if tail:
            repair_marker = 'CANDIDATE AND ATTRIBUTED MODEL FINDINGS (JSON):\n'
            if not tail.startswith(('BOUNDED REVISION REPAIR:', 'BOUNDED DRAFT REPAIR:')) or repair_marker not in tail:
                raise ValueError('Unexpected text after the recorded proposal context.')
            payload = {'context': context, 'repair': json.loads(tail.split(repair_marker, 1)[1])}
    for source in context['evidence_packet']['sources']:
        if source.get('layer') == 'investigator_decision':
            from grantlib.studio import decision_source
            allowed = (allowed_decision_texts if allowed_decision_texts is not None else
                       [json.loads(FIXTURE.read_text())['change']['decision']])
            matches = [d for d in context.get('investigator_decisions', [])
                       if d.get('id') == source.get('id') and d.get('text') in allowed]
            if len(matches) != 1 or source != decision_source(matches[0]['proposal_id'], matches[0]):
                raise ValueError('Refusing to send non-fictional or altered decision evidence.')
            continue
        if (source.get('metadata', {}).get('fictional') is not True
                or not source.get('source_uri', '').startswith('urn:fictional-benchmark:')
                or not source.get('text', '').startswith('FICTIONAL BENCHMARK:')):
            raise ValueError('Refusing to send non-fictional evidence.')
    return payload


def guard_draft_audit(payload, *, stage, initial_candidate, repaired_candidate=None):
    """Bind fresh audit payloads to immutable generated outputs, not external data."""
    from grantlib.draft_check import baseline_snapshot
    if stage == 'check-initial':
        if 'repair_baseline' in payload:
            raise ValueError('An initial draft audit must not receive a repair baseline.')
        if payload.get('candidate') != initial_candidate:
            raise ValueError('The initial audit candidate differs from the recorded generated output.')
    elif stage == 'check-final':
        expected = baseline_snapshot(initial_candidate)
        if payload.get('repair_baseline') != expected:
            raise ValueError('The final audit repair baseline differs from the exact initial generated output.')
        if repaired_candidate is None or payload.get('candidate') != repaired_candidate:
            raise ValueError('The final audit candidate differs from the recorded repair output.')
    else:
        raise ValueError('A draft audit guard requires an initial or final check stage.')


def guard_audit_context(payload, *, generation_context):
    """Require only the production-defined historical-opinion exclusions."""
    from grantlib.audit_context import prepare
    expected, _ = prepare(generation_context)
    if payload.get('context') != expected:
        raise ValueError('The audit context differs from the exact cleaned generation context.')


def run_evaluation(output_dir, *, generate, mode='injected', timeout=900, audit_contract='legacy'):
    """Execute four observed jobs with a supplied generator.

    Tests may inject a synthetic generator. The CLI supplies model.generate only
    when --run-model is present. There is no default generator or archive lookup.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be positive')
    if audit_contract not in AUDIT_CONTRACTS:
        raise ValueError('Unknown evaluation audit contract.')
    output_dir = Path(output_dir).resolve()
    fixture_bytes = (output_dir / 'fixture.json').read_bytes()
    fixture = json.loads(fixture_bytes)
    validate_fixture(fixture)
    state = json.loads((output_dir / 'summary.json').read_text(encoding='utf-8'))
    if state['status'] != 'prepared' or (output_dir / 'studio.sqlite3').exists():
        raise ValueError('Use a freshly prepared output directory; runs are not resumed or overwritten.')
    if state.get('audit_contract', 'legacy') != audit_contract:
        raise ValueError('The requested audit contract differs from the prepared evaluation.')
    fixture_hash = state.get('prepared_fixture_sha256')
    if fixture_hash is None and audit_contract != 'legacy':
        raise ValueError('The experimental evaluation requires its prepared fixture hash.')
    if fixture_hash is not None and fixture_hash != hashlib.sha256(fixture_bytes).hexdigest():
        raise ValueError('The evaluation fixture differs from its prepared snapshot.')
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    from grantlib.studio import Service, export_markdown
    draft_factory = revision_factory = None
    if audit_contract == 'scientific_defects_v1':
        from grantlib import audit_contract as contract_requests
        draft_factory = contract_requests.prepare_draft
        revision_factory = contract_requests.prepare_revision

    state.update(status='running', generation_mode=mode, started_at=stamp(), jobs=[], calls=[],
                 audit_contract=audit_contract,
                 expected_call_range=[MIN_CALLS, MAX_CALLS],
                 studio_code_sha256=hashlib.sha256((REPO / 'grantlib/studio.py').read_bytes()).hexdigest())
    started = time.monotonic()
    active = {'job_id': None, 'prefix': None, 'action': None, 'changed': False}
    summary_lock = threading.Lock()
    sentinel = output_dir / 'synthetic-context-only.sqlite3'
    # Serialize snapshots immediately on generation, so a caller retaining the
    # returned mutable dict cannot silently replace a later audit's baseline.
    generated_candidates = {}
    generation_contexts = {}

    def event(value):
        with (output_dir / 'events.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps({'at': stamp(), **value}) + '\n')
        (output_dir / 'events.jsonl').chmod(0o600)
        print(json.dumps(value), flush=True)

    def persist():
        with summary_lock:
            write_json(output_dir / 'summary.json', state)

    def packet_builder(db_path, _brief):
        # Service requires a path argument, but this provider never connects to it.
        if Path(db_path) != sentinel or sentinel.exists():
            raise ValueError('An archive connection is not permitted in this evaluation.')
        sources = copy.deepcopy(fixture['initial_sources'])
        if active['changed']:
            sources = [s for s in sources if s['id'] != fixture['change']['superseded_source_id']]
            sources.append(copy.deepcopy(fixture['change']['source']))
        return {'version': 1, 'generated_at': stamp(), 'sources': sources,
                'coverage': {'synthetic_only': True, 'real_archive_opened': False,
                             'source_count': len(sources), 'generation_context_limit_chars': 140000},
                'warnings': ['FICTIONAL BENCHMARK: invented material, not scientific evidence.',
                             'No real funding requirements or scientific references were verified.']}

    def recorded_generate(prompt, **kwargs):
        payload = prompt_data(prompt, allowed_decision_texts=[fixture['change']['decision']])
        relative = Path(kwargs['run_dir']).resolve().relative_to(output_dir / 'studio-runs')
        if active['action'] in CHECKED_ACTIONS:
            if len(relative.parts) != 2 or relative.parts[1] not in REVISION_STAGES:
                raise ValueError('Unexpected checked drafting stage directory.')
            stage = relative.parts[1]
            previous = [c['stage'] for c in state['calls'] if c['job_id'] == relative.parts[0]]
            if len(previous) >= len(REVISION_STAGES) or stage != REVISION_STAGES[len(previous)]:
                raise ValueError('Draft/check stages must run once, in order, with at most one repair per job.')
            if stage.startswith('check-'):
                if relative.parts[0] not in generation_contexts:
                    raise ValueError('The audit has no immutable generation-context snapshot.')
                guard_audit_context(payload, generation_context=json.loads(generation_contexts[relative.parts[0]]))
                expected_marker = audit_marker(active['action'], audit_contract)
                if prompt.splitlines()[0] != expected_marker:
                    raise ValueError('The scientific check uses the wrong audit protocol.')
                if active['action'] != 'revise' and payload.get('action') != active['action']:
                    raise ValueError('The scientific check targets a different drafting action.')
                if active['action'] != 'revise':
                    recorded = generated_candidates.get(relative.parts[0], {})
                    if 'draft' not in recorded:
                        raise ValueError('The draft audit has no immutable initial generation snapshot.')
                    guard_draft_audit(payload, stage=stage, initial_candidate=json.loads(recorded['draft']),
                                      repaired_candidate=json.loads(recorded['repair']) if 'repair' in recorded else None)
                if audit_contract == 'scientific_defects_v1':
                    if active['action'] == 'revise':
                        expected = revision_factory(payload['context'], payload['candidate'])
                    else:
                        baseline = payload.get('repair_baseline')
                        expected = draft_factory(payload['context'], payload['candidate'], action=active['action'],
                            repair_baseline=baseline['candidate'] if baseline is not None else None)
                    if prompt != expected.prompt or kwargs['schema'] != expected.schema:
                        raise ValueError('The scientific check differs from the exact prepared quote contract.')
            elif stage == 'draft':
                generation_contexts[relative.parts[0]] = json.dumps(payload, ensure_ascii=False,
                                                                   sort_keys=True, allow_nan=False)
            elif payload.get('context') != json.loads(generation_contexts[relative.parts[0]]):
                raise ValueError('The repair context differs from the original generation context.')
        else:
            if len(relative.parts) != 1 or any(c['action'] == active['action'] for c in state['calls']):
                raise ValueError('Independent review permits one generation call.')
            stage = 'generate'
        if state['model_calls'] >= MAX_CALLS or any(c['status'] == 'running' for c in state['calls']):
            raise ValueError('This evaluation permits at most thirteen sequential generation calls.')
        call_prefix = f'{state["model_calls"] + 1:02d}-{active["action"]}-{stage}'
        call_dir = output_dir / 'calls' / call_prefix
        call_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        write_text(call_dir / 'input.txt', prompt)
        write_json(call_dir / 'input.json', payload)
        write_json(call_dir / 'schema.json', kwargs['schema'])
        marker = prompt.splitlines()[0] if stage.startswith('check-') else None
        call = {'index': state['model_calls'] + 1, 'action': active['action'], 'stage': stage,
                'job_id': relative.parts[0], 'run_dir': str(relative),
                'protocol': (AUDIT_PROTOCOLS[marker] if marker else
                             'bounded_repair' if 'repair' in payload else 'generation'),
                'audit_contract': audit_contract if marker else None, 'audit_marker': marker,
                'files': str(call_dir.relative_to(output_dir)), 'status': 'running',
                'started_at': stamp(), 'input_sha256': hashlib.sha256(prompt.encode()).hexdigest()}
        state['calls'].append(call)
        state['model_calls'] += 1
        persist()
        kwargs['timeout'] = min(timeout, kwargs.get('timeout', timeout))
        began = time.monotonic()
        try:
            result = generate(prompt, **kwargs)
            write_json(call_dir / 'output.json', result)
            if active['action'] != 'revise' and stage in ('draft', 'repair') and result.get('status') == 'completed':
                candidate = result.get('structured')
                if candidate is None:
                    candidate = json.loads(result['text'])
                generated_candidates.setdefault(relative.parts[0], {})[stage] = json.dumps(
                    candidate, ensure_ascii=False, sort_keys=True, allow_nan=False)
            call.update({key: result.get(key) for key in ('status', 'provider', 'model', 'usage', 'run_id')})
            return result
        except BaseException as exc:
            call.update(status='failed', error=f'{type(exc).__name__}: {exc}'[:1600])
            raise
        finally:
            call['elapsed_seconds'] = round(time.monotonic() - began, 3)
            write_json(call_dir / 'observation.json', call)
            persist()

    service = Service(sentinel, generate=recorded_generate, packet_builder=packet_builder,
                      draft_audit_factory=draft_factory, revision_audit_factory=revision_factory)
    proposal = service.store.create(fixture['brief'])
    state['proposal_id'] = proposal['id']
    persist()

    def execute(action, **arguments):
        current = service.store.get(proposal['id'])
        prefix = f'{len(state["jobs"]) + 1:02d}-{action}'
        active['prefix'] = prefix
        active['action'] = action
        request = {'proposal_id': current['id'], 'expected_revision': current['revision'],
                   'action': action, 'instruction': fixture['instructions'][action], **arguments}
        write_json(output_dir / f'{prefix}-request.json', request)
        job = service.start(**request)
        active['job_id'] = job['id']
        record = {'id': job['id'], 'action': action, 'status': job['status']}
        state['jobs'].append(record)
        persist()
        began, previous = time.monotonic(), None
        cancellation_deadline = None
        while True:
            job = service.store.job(job['id'])
            observed = job['status'], job['phase']
            if observed != previous:
                event({'job_id': job['id'], 'action': action, 'status': job['status'], 'phase': job['phase']})
                previous = observed
            if job['status'] not in ('queued', 'running'):
                break
            if time.monotonic() - began > timeout + 15 and cancellation_deadline is None:
                service.cancel(job['id'])
                cancellation_deadline = time.monotonic() + 30
            if cancellation_deadline is not None and time.monotonic() > cancellation_deadline:
                raise TimeoutError('The generator did not stop within the cancellation grace period.')
            time.sleep(.2)
        terminal = service.store.job(job['id'], True)
        write_json(output_dir / f'{prefix}-job.json', terminal)
        record.update(status=job['status'], error=job['error'], elapsed_seconds=round(time.monotonic() - began, 3))
        persist()
        active['job_id'] = None
        if job['status'] != 'completed':
            raise RuntimeError(f'{action} ended as {job["status"]}; inspect {prefix}-job.json.')
        current = service.store.get(proposal['id'])
        artifact = next(a for a in current['artifacts'] if a['id'] == job['result_artifact_id'])
        write_json(output_dir / f'{prefix}-artifact.json', artifact)
        write_text(output_dir / f'{prefix}.md', export_markdown(current, artifact['id']))
        record.update(artifact_id=artifact['id'], citation_check=artifact['citation_check'],
                      model_metadata=artifact['model_metadata'], word_counts=word_counts(artifact['markdown'], artifact.get('structured')),
                      calls=[c['index'] for c in state['calls'] if c['job_id'] == job['id']])
        persist()
        return artifact

    try:
        design = execute('design')
        direction = design['structured']['directions'][0]['id']
        state['selected_direction'] = direction
        aims = execute('aims', base_artifact_id=design['id'], selected_direction=direction)
        review = execute('review', base_artifact_id=aims['id'], selected_direction=direction)
        current = service.store.get(proposal['id'])
        current = service.store.decision(current['id'], current['revision'], fixture['change']['decision'])
        brief = copy.deepcopy(current['brief'])
        brief['notes'] += fixture['change']['notes_append']
        brief['constraints'] = fixture['change']['constraints']
        service.store.update(current['id'], current['revision'], brief)
        active['changed'] = True
        write_json(output_dir / 'recorded-change.json', {'decisions': current['decisions'], 'updated_brief': brief})
        before_revision = service.store.get(proposal['id'])
        write_json(output_dir / 'proposal-before-revision.json', before_revision)
        revised = execute('revise', base_artifact_id=review['id'], selected_direction=direction)
        final = service.store.get(proposal['id'])
        state.update(status='completed', artifact_count=len(final['artifacts']),
                     revision_targets_reviewed_aims=revised['base_artifact_id'] == aims['id'],
                     investigator_decisions_unchanged=final['decisions'] == before_revision['decisions'],
                     investigator_brief_unchanged=final['brief'] == before_revision['brief'],
                     revision_check=revised['model_metadata'].get('revision_check'),
                     earlier_artifacts_unchanged=all(a == next(v for v in final['artifacts'] if v['id'] == a['id'])
                                                     for a in (design, aims, review)))
    except BaseException as exc:
        state.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                     error=f'{type(exc).__name__}: {exc}')
        raise
    finally:
        if active['job_id']:
            service.cancel(active['job_id'])
        service.shutdown()
        if active['job_id']:
            job = service.store.job(active['job_id'], True)
            write_json(output_dir / f'{active["prefix"]}-job.json', job)
            state['jobs'][-1].update(status=job['status'], error=job['error'])
        final = service.store.get(proposal['id'])
        write_json(output_dir / 'proposal-final.json', final)
        state.update(finished_at=stamp(), elapsed_seconds=round(time.monotonic() - started, 3),
                     all_jobs_terminal=all(j['status'] not in ('queued', 'running') for j in state['jobs']))
        stages = {action: [c['stage'] for c in state['calls'] if c['action'] == action]
                  for action in ('design', 'aims', 'review', 'revise')}
        state['call_plan_satisfied'] = (
            MIN_CALLS <= state['model_calls'] <= MAX_CALLS
            and stages['review'] == ['generate']
            and all(values[:2] == ['draft', 'check-initial']
                    and values == list(REVISION_STAGES[:len(values)])
                    for action, values in stages.items() if action != 'review'))
        state['total_usage'] = {}
        for call in state['calls']:
            for key, value in (call.get('usage') or {}).items():
                if isinstance(value, (int, float)):
                    state['total_usage'][key] = state['total_usage'].get(key, 0) + value
        persist()
        event({'status': state['status'], 'output_dir': str(output_dir), 'all_jobs_terminal': state['all_jobs_terminal']})
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True, type=Path,
                        help='New private directory under an existing parent; existing paths are refused.')
    parser.add_argument('--run-model', action='store_true',
                        help='Run four jobs with 7–13 model calls, including automatic draft checks and at most one repair per drafting job. Default: prepare only.')
    parser.add_argument('--timeout', type=float, default=900,
                        help='Job observation budget and maximum seconds per model call (default: 900).')
    parser.add_argument('--audit-contract', choices=AUDIT_CONTRACTS, default='legacy',
                        help='Automatic check contract: legacy by default; scientific_defects_v1 explicitly opts in to experimental quote audits.')
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error('--timeout must be positive')
    try:
        output_dir = prepare(args.output_dir, audit_contract=args.audit_contract)
        if not args.run_model:
            print(json.dumps({'status': 'prepared', 'model_calls': 0, 'output_dir': str(output_dir),
                              'audit_contract': args.audit_contract}))
            return 0
        if str(REPO) not in sys.path:
            sys.path.insert(0, str(REPO))
        from grantlib.model import generate
        run_evaluation(output_dir, generate=generate, mode='model', timeout=args.timeout,
                       audit_contract=args.audit_contract)
        return 0
    except KeyboardInterrupt:
        print('Evaluation interrupted; inspect the saved job status.', file=sys.stderr)
        return 130
    except (OSError, ValueError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
