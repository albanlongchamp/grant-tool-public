"""Headless CLI entry points for persistent grant-development workflows."""
import json
from pathlib import Path
import sys

from .studio import Service, Store, export_markdown


def register(sub):
    parent = sub.add_parser('proposal', help='Design, draft, review and revise a private proposal')
    actions = parent.add_subparsers(dest='proposal_command', required=True)
    actions.add_parser('list')
    actions.add_parser('engine')
    job = actions.add_parser('job', help='Inspect the existing durable job; never restart it')
    job.add_argument('job_id')
    cancel = actions.add_parser('cancel', help='Request cancellation of the existing job')
    cancel.add_argument('job_id')
    create = actions.add_parser('create')
    create.add_argument('--brief', required=True, help='Local JSON file containing the proposal brief')
    show = actions.add_parser('show')
    show.add_argument('id')
    show.add_argument('--artifact')
    show.add_argument('--with-evidence', action='store_true')
    gather = actions.add_parser('evidence')
    gather.add_argument('id')
    run = actions.add_parser('run')
    run.add_argument('id')
    run.add_argument('--action', required=True, choices=['design','aims','strategy','review','revise'])
    run.add_argument('--instruction', default='')
    run.add_argument('--direction', default='')
    run.add_argument('--base-artifact')
    run.add_argument('--min-words', type=int, help='Explicit minimum main-draft words (local counting convention)')
    run.add_argument('--max-words', type=int, help='Explicit maximum main-draft words (local counting convention)')
    run.add_argument('--provider', choices=['codex', 'command'], default='codex')
    run.add_argument('--model', help='Requested model ID; recorded separately from provider-reported identity')
    run.add_argument('--adapter-file', help='Trusted local JSON command adapter configuration; see docs/HARNESS.md')
    for task in (gather, run):
        task.add_argument('--jsonl', action='store_true', help='Emit typed JSON events only on stdout')
    decision = actions.add_parser('decide')
    decision.add_argument('id')
    decision.add_argument('--text', required=True)
    save = actions.add_parser('save')
    save.add_argument('id')
    save.add_argument('--file', required=True)
    save.add_argument('--title', default='Edited draft')
    save.add_argument('--base-artifact')
    save.add_argument('--author', choices=['human','assistant'], default='human')
    export = actions.add_parser('export')
    export.add_argument('id')
    export.add_argument('--artifact', required=True)
    export.add_argument('--output', required=True)
    export.add_argument('--format', choices=['md', 'docx'], help='Default: inferred from the output suffix')
    reference = actions.add_parser('reference', help='Resolve an explicit PMID or DOI using public bibliographic APIs')
    reference.add_argument('id')
    reference.add_argument('--identifier', required=True)
    fulltext = actions.add_parser('fulltext', help='Fetch one explicit accessible public PMC article snapshot')
    fulltext.add_argument('id')
    fulltext.add_argument('--pmcid', required=True)
    reader = actions.add_parser('fulltext-show', help='Read a retained article snapshot locally; no raw XML bytes')
    reader.add_argument('id')
    reader.add_argument('--document', required=True)
    retain = actions.add_parser('retain-passage', help='Retain one exact whole paragraph as publication evidence')
    retain.add_argument('id')
    retain.add_argument('--document', required=True)
    retain.add_argument('--paragraph', required=True)
    claim_list = actions.add_parser('claims', help='Show version-specific claims and recorded judgments')
    claim_list.add_argument('id')
    claim_list.add_argument('--artifact', required=True)
    add_claim = actions.add_parser('add-claim', help='Select an exact draft quotation for review')
    add_claim.add_argument('id')
    add_claim.add_argument('--artifact', required=True)
    add_claim.add_argument('--quote', required=True)
    assess = actions.add_parser('assess', help='Record a source-grounded judgment about one version-specific claim')
    assess.add_argument('id')
    assess.add_argument('--artifact', required=True)
    assess.add_argument('--claim', required=True)
    assess.add_argument('--source', default='')
    assess.add_argument('--verdict', required=True, choices=['supported', 'partial', 'contradicted', 'unresolved'])
    assess.add_argument('--excerpt-file', help='UTF-8 text file containing an exact source or abstract excerpt')
    assess.add_argument('--locator', default='')
    assess.add_argument('--note', required=True)
    assess.add_argument('--reviewer', choices=['investigator', 'assistant'], default='assistant')
    for mutation in (gather, run, decision, save, reference, fulltext, retain, add_claim, assess):
        mutation.add_argument('--expected-revision', type=int,
                              help='Require this observed proposal revision; omit for interactive latest-revision convenience')


def summary(proposal, with_evidence=False):
    result = dict(proposal)
    result['artifacts'] = [{k: a.get(k) for k in ('id','kind','title','created_at','origin','base_artifact_id','citation_check')} for a in proposal['artifacts']]
    if not with_evidence:
        context = proposal.get('context')
        result['context'] = {'source_count': len(context.get('sources', [])), 'coverage': context.get('coverage'), 'warnings': context.get('warnings')} if context else None
        result['references'] = [{k: r.get(k) for k in ('id', 'title', 'canonical', 'provider', 'retrieved_at', 'verification')} for r in proposal.get('references', [])]
        result['publication_passages'] = [{k: s.get(k) for k in ('id', 'title', 'layer', 'source_uri', 'metadata')}
                                          for s in proposal.get('publication_passages', [])]
    return result


def dispatch(args, config):
    database = Path(config['data_dir']) / 'library.sqlite3'
    store = Store(database.parent)
    action = args.proposal_command
    def revision(proposal):
        value = getattr(args, 'expected_revision', None)
        if value is not None and value < 1:
            raise ValueError('Expected revision must be a positive integer.')
        return proposal['revision'] if value is None else value
    if action == 'engine':
        from .model import availability
        result = availability()
    elif action == 'list':
        result = store.list()
    elif action == 'job':
        store.recover()
        result = store.job(args.job_id)
    elif action == 'cancel':
        store.recover()
        result = store.request_cancel(args.job_id)
    elif action == 'create':
        result = summary(store.create(json.loads(Path(args.brief).read_text())))
    elif action == 'show':
        proposal = store.get(args.id)
        if args.artifact:
            result = next((dict(a) for a in proposal['artifacts'] if a['id'] == args.artifact), None)
            if result is None:
                raise ValueError('Version not found in this proposal.')
            if not args.with_evidence:
                result.pop('context', None)
        else:
            result = summary(proposal, args.with_evidence)
    elif action == 'decide':
        p = store.get(args.id)
        result = summary(store.decision(args.id, revision(p), args.text))
    elif action == 'save':
        p = store.get(args.id)
        result = summary(store.save(args.id, revision(p), Path(args.file).read_text(),
                                    args.title, args.base_artifact, 'assistant_import' if args.author == 'assistant' else 'human'))
    elif action == 'export':
        target = Path(args.output)
        output_format = args.format or target.suffix.lstrip('.').lower()
        if output_format not in ('md', 'docx'):
            raise ValueError('Use an .md or .docx output path, or specify --format.')
        p = store.get(args.id)
        if output_format == 'docx':
            from .docx_export import export_docx
            content = export_docx(p, args.artifact)
        else:
            content = export_markdown(p, args.artifact).encode('utf-8')
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        result = {'output': str(target.resolve())}
    elif action == 'reference':
        from .references import ReferenceError
        p = store.get(args.id)
        try:
            result = summary(Service(database).resolve_reference(args.id, revision(p), args.identifier))
        except ReferenceError as exc:
            raise ValueError(str(exc)) from exc
    elif action == 'fulltext':
        from .references import ReferenceError
        p = store.get(args.id)
        try:
            fetched = Service(database).fetch_full_text(args.id, revision(p), args.pmcid)
            result = {'proposal': summary(fetched['proposal']), 'document_id': fetched['document_id']}
        except ReferenceError as exc:
            raise ValueError(str(exc)) from exc
    elif action == 'fulltext-show':
        result = store.full_text(args.id, args.document)
    elif action == 'retain-passage':
        p = store.get(args.id)
        result = summary(store.retain_publication_passage(args.id, revision(p), args.document, args.paragraph))
    elif action == 'claims':
        p = store.get(args.id)
        artifact = next((a for a in p['artifacts'] if a['id'] == args.artifact), None)
        if artifact is None:
            raise ValueError('Version not found in this proposal.')
        result = {'artifact_id': args.artifact, 'claims': artifact['claims'], 'references': p['references'],
                  'publication_passages': p['publication_passages']}
    elif action == 'add-claim':
        p = store.get(args.id)
        result = summary(store.add_claim(args.id, revision(p), args.artifact, args.quote))
    elif action == 'assess':
        p = store.get(args.id)
        excerpt = Path(args.excerpt_file).read_text(encoding='utf-8') if args.excerpt_file else ''
        result = summary(store.assess_claim(args.id, revision(p), args.artifact, args.claim,
                                           source_id=args.source, verdict=args.verdict, excerpt=excerpt,
                                           locator=args.locator, note=args.note, reviewer=args.reviewer))
    elif action in ('evidence', 'run'):
        from .harness import GrantHarness
        provider = _provider(args) if action == 'run' else None
        jsonl = getattr(args, 'jsonl', False)
        observed = {}
        def event(item):
            if item['type'] == 'started':
                observed['job_id'] = item['job']['id']
            if jsonl:
                if item['type'] == 'result' and item['job']['status'] != 'completed':
                    item = {'type': 'error', 'code': 'cancelled' if item['job']['status'] == 'cancelled' else 'task_failed',
                            'message': item['job'].get('error') or 'The proposal task did not complete.', 'job': item['job']}
                print(json.dumps(item, ensure_ascii=False), flush=True)
            elif item['type'] == 'started':
                print('Job ' + item['job']['id'], file=sys.stderr, flush=True)
            elif item['type'] == 'phase':
                print(item['phase'], file=sys.stderr, flush=True)
        with GrantHarness(database, provider=provider) as harness:
            p = harness.get(args.id)
            try:
                result = harness.run(args.id, 'evidence' if action == 'evidence' else args.action,
                                     expected_revision=revision(p), on_event=event,
                                     instruction=getattr(args, 'instruction', ''),
                                     selected_direction=getattr(args, 'direction', ''),
                                     base_artifact_id=getattr(args, 'base_artifact', None),
                                     min_words=getattr(args, 'min_words', None),
                                     max_words=getattr(args, 'max_words', None))
            except KeyboardInterrupt:
                if not observed.get('job_id'):
                    raise ValueError('Interrupted before a task handle was observed.') from None
                harness.cancel(observed['job_id'])
                result = harness.wait(observed['job_id'], on_event=event)
        if not jsonl:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['status'] == 'completed' else 1
    else:
        raise ValueError('Unknown proposal command.')
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _provider(args):
    from .providers import CodexProvider, CommandProvider
    kind = getattr(args, 'provider', 'codex')
    adapter_file = getattr(args, 'adapter_file', None)
    model = getattr(args, 'model', None)
    if kind == 'codex':
        if adapter_file:
            raise ValueError('--adapter-file requires --provider command.')
        return CodexProvider(model=model)
    if not adapter_file:
        raise ValueError('--provider command requires an explicit trusted --adapter-file.')
    data = Path(adapter_file).read_bytes()
    if len(data) > 32768:
        raise ValueError('The adapter configuration exceeds 32 KB.')
    config = json.loads(data)
    if not isinstance(config, dict) or set(config) - {'provider', 'command'} or 'command' not in config:
        raise ValueError('Adapter configuration must contain command and optionally provider; credentials belong in the runner authentication environment.')
    return CommandProvider(config['command'], provider=config.get('provider', 'command'), model=model)
