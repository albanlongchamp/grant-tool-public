"""Explicitly recorded, local research memory; never infer investigator approval."""
import json
from pathlib import Path


def register(sub):
    parent = sub.add_parser('memory', help='Manage reusable, explicitly confirmed research knowledge')
    actions = parent.add_subparsers(dest='memory_command', required=True)
    actions.add_parser('list', help='Read current records, history and catalog revision')
    for name in ('create', 'revise'):
        action = actions.add_parser(name)
        if name == 'revise':
            action.add_argument('entry_id')
        action.add_argument('--file', required=True, help='JSON record with text, scope, provenance_note and status')
        action.add_argument('--expected-catalog-revision', required=True, type=int)
        action.add_argument('--actor', choices=['assistant', 'investigator'], default='assistant',
                            help='Use investigator only to record an actual investigator statement/confirmation')
    choose = actions.add_parser('select', help='Select exact confirmed versions for one proposal')
    choose.add_argument('proposal_id')
    choose.add_argument('--file', required=True, help='JSON list of {entry_id,version_id,note}; [] clears the selection')
    choose.add_argument('--expected-revision', required=True, type=int)
    choose.add_argument('--expected-catalog-revision', required=True, type=int)


def dispatch(args, config):
    from .studio import Store
    store = Store(Path(config['data_dir']))
    action = args.memory_command
    if action == 'list':
        result = store.memory_catalog()
    elif action in ('create', 'revise'):
        data = json.loads(Path(args.file).read_text(encoding='utf-8'))
        if action == 'create':
            result = store.create_memory(args.expected_catalog_revision, data, actor=args.actor)
        else:
            result = store.revise_memory(args.entry_id, args.expected_catalog_revision, data, actor=args.actor)
    elif action == 'select':
        selections = json.loads(Path(args.file).read_text(encoding='utf-8'))
        proposal = store.select_memory(args.proposal_id, args.expected_revision, args.expected_catalog_revision, selections)
        result = {key: proposal[key] for key in ('id', 'revision', 'memory_bindings')}
    else:
        raise ValueError('Unknown memory command.')
    print(json.dumps(result, ensure_ascii=False, indent=2))
