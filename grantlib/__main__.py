"""Command-line entry point: python3 -m grantlib."""
import argparse
import json
from pathlib import Path
import sys

from .model import ModelError

from .library import (
    EVIDENCE_STATUSES, ROLES, curate_document, curate_unit, get_document,
    ingest, list_documents, load_config, search, stats, write_brief,
)


def _filters(parser):
    parser.add_argument('--role', default='own_grant', choices=sorted(ROLES | {'all'}))
    for key in ('category', 'topic', 'technique', 'cycle', 'status', 'project'):
        parser.add_argument('--' + key)
    parser.add_argument('--limit', type=int, default=20)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Local source-linked grant knowledge library')
    parser.add_argument('--config', default='config.local.json')
    sub = parser.add_subparsers(dest='command', required=True)
    from .studio_cli import register as register_studio
    register_studio(sub)
    from .memory_cli import register as register_memory
    register_memory(sub)
    from .refresh_cli import register as register_refresh
    register_refresh(sub)
    sub.add_parser('ingest', help='Inventory and index source files without changing them')
    sub.add_parser('stats', help='Show coverage and annotation counts')
    sub.add_parser('inventory', help='List every inventoried source file')
    find = sub.add_parser('search', help='Search original passages with synonyms and filters')
    find.add_argument('query')
    _filters(find)
    find.add_argument('--json', action='store_true')
    brief = sub.add_parser('brief', help='Export a source-linked drafting brief')
    brief.add_argument('query')
    brief.add_argument('--output', required=True)
    _filters(brief)
    view = sub.add_parser('document')
    view.add_argument('id')
    review = sub.add_parser('curate', help='Persist an explicitly reviewed evidence status')
    review.add_argument('id')
    review.add_argument('--status', required=True, choices=sorted(EVIDENCE_STATUSES))
    review.add_argument('--note', required=True)
    label = sub.add_parser('label-document', help='Correct document ownership classification')
    label.add_argument('id')
    label.add_argument('--role', required=True, choices=sorted(ROLES))
    nih_fetch = sub.add_parser('nih-fetch', help='Fetch a bounded set of public NIH project descriptions')
    nih_fetch.add_argument('--topic', required=True, help='Public research keywords only')
    nih_fetch.add_argument('--years', type=int, nargs='+', required=True)
    nih_fetch.add_argument('--max-records', type=int, default=100)
    nih_fetch.add_argument('--search-fields', default='projecttitle,abstracttext,terms',
                           choices=['projecttitle,abstracttext,terms','projecttitle,abstracttext','projecttitle','abstracttext'])
    nih_find = sub.add_parser('nih-search')
    nih_find.add_argument('query')
    nih_find.add_argument('--year', type=int)
    nih_find.add_argument('--activity')
    nih_find.add_argument('--limit', type=int, default=20)
    sub.add_parser('nih-stats')
    nih_import = sub.add_parser('nih-import', help='Import official ExPORTER project/abstract CSV or ZIP files')
    nih_import.add_argument('--projects', nargs='+', required=True)
    nih_import.add_argument('--abstracts', nargs='+', required=True)
    guidance = sub.add_parser('guidance', help='Find official government grant-writing advice')
    guidance.add_argument('query', nargs='?', default='')
    guidance.add_argument('--mechanism')
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        db = Path(config['data_dir']) / 'library.sqlite3'
        filters = {k: getattr(args, k, None) for k in ('role', 'category', 'topic', 'technique', 'cycle', 'status', 'project')}
        if args.command.startswith('refresh'):
            from .refresh_cli import dispatch
            status = dispatch(args, config)
            if status:
                parser.exit(status)
        elif args.command == 'proposal':
            from .studio_cli import dispatch
            status = dispatch(args, config)
            if status:
                parser.exit(status)
        elif args.command == 'memory':
            from .memory_cli import dispatch
            dispatch(args, config)
        elif args.command == 'ingest':
            result = ingest(config, progress=lambda s: print(s, flush=True))
            print(json.dumps(result['counts'], indent=2))
        elif args.command == 'stats':
            print(json.dumps(stats(db), indent=2))
        elif args.command == 'inventory':
            print(json.dumps(list_documents(db, limit=10000), indent=2))
        elif args.command == 'search':
            result = search(db, args.query, filters, args.limit)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                for item in result:
                    print(f"\n[{item['id']}] {item['relative_path']} — {item['locator']}")
                    print(f"{item['evidence_status']} | {', '.join(item['categories'])} | {item['source_uri']}")
                    print(item['text'])
                print(f'\n{len(result)} distinct passages shown.')
        elif args.command == 'document':
            print(json.dumps(get_document(db, args.id), indent=2, ensure_ascii=False))
        elif args.command == 'brief':
            count = write_brief(db, args.query, args.output, filters, args.limit)
            print(f'Wrote {count} source passages to {args.output}')
        elif args.command == 'curate':
            curate_unit(db, args.id, args.status, args.note)
            print('Review saved to the library and persistent curation file.')
        elif args.command == 'label-document':
            curate_document(db, args.id, args.role)
            print('Document role updated; reingest to refresh dependent annotations.')
        elif args.command.startswith('nih-'):
            from . import nih
            nih_db = Path(config['data_dir']) / 'nih.sqlite3'
            if args.command == 'nih-fetch':
                result = nih.fetch_pilot(nih_db, fiscal_years=args.years, public_topic=args.topic,
                                        max_records=args.max_records, search_fields=args.search_fields)
            elif args.command == 'nih-search':
                result = nih.search(nih_db, args.query, limit=args.limit, fiscal_year=args.year, activity=args.activity)
            elif args.command == 'nih-stats':
                result = nih.stats(nih_db)
            else:
                result = nih.import_exporter(nih_db, project_files=args.projects, abstract_files=args.abstracts)
            print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        elif args.command == 'guidance':
            from .guidance import search_guidance
            print(json.dumps(search_guidance(args.query, mechanism=args.mechanism), indent=2, ensure_ascii=False))
    except (ValueError, OSError, KeyError, ModelError) as error:
        if getattr(args, 'jsonl', False):
            from .studio import Conflict
            code = getattr(error, 'code', 'conflict' if isinstance(error, Conflict) else 'invalid_request')
            print(json.dumps({'type': 'error', 'code': code, 'message': str(error)}, ensure_ascii=False), flush=True)
            parser.exit(1)
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
