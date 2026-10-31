#!/usr/bin/env python3
"""Check staged Git content and optional history for private release artifacts.

This reads Git objects, not ignored runtime files or private grants.
It is a limited release aid, not a substitute for review of names and prose.
"""
import argparse
from pathlib import PurePosixPath
import re
import subprocess
import sys


PATTERNS = {
    'private key': re.compile(rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'GitHub token': re.compile(rb'\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b'),
    'cloud access key': re.compile(rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'provider token': re.compile(rb'\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{32,}\b'),
    'personal home path': re.compile(rb'(?<![A-Za-z0-9.])/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'Windows home path': re.compile(rb'(?i)[A-Z]:[\\/]+(?:Users|Documents and Settings)[\\/]+[^\\/\r\n\"]+[\\/]'),
    'encoded home path': re.compile(rb'(?i)(?:%2f|\\u002f)(?:Users|home)(?:%2f|\\u002f)[A-Za-z0-9_.-]+(?:%2f|\\u002f)'),
}
PRIVATE_PARTS = {'.local', '.venv', 'venv', '__pycache__', 'private', 'source-documents', 'exports',
                 '.ssh', '.aws', '.codex', '.gnupg'}
PRIVATE_SUFFIXES = {'.sqlite', '.sqlite3', '.db', '.pem', '.key', '.log', '.pyc',
                    '.doc', '.docx', '.pdf', '.rtf', '.zip'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--history', action='store_true',
                        help='Also inspect all reachable commits, deleted files and annotated tags; requires full Git history.')
    args = parser.parse_args(argv)
    if args.history and subprocess.check_output(['git', 'rev-parse', '--is-shallow-repository']).strip() == b'true':
        print('History check requires a complete clone; fetch full history first.', file=sys.stderr)
        return 1
    result = subprocess.run(['git', 'ls-files', '--stage', '-z'], capture_output=True, check=True)
    failures = []
    count = 0
    contents = {}

    def content_for(object_id, kind='blob'):
        key = (kind, object_id)
        if key not in contents:
            contents[key] = subprocess.check_output(['git', 'cat-file', kind, object_id.decode('ascii')])
        return contents[key]

    def scan_patterns(label, content):
        for reason, pattern in PATTERNS.items():
            if pattern.search(content):
                failures.append((label, reason))

    def path_label(raw_path):
        if any(pattern.search(raw_path) for pattern in PATTERNS.values()):
            return '[redacted filename]'
        return raw_path.decode('utf-8', errors='replace')

    def scan_file(raw_path, mode, object_id, suffix=''):
        name = raw_path.decode('utf-8', errors='replace')
        label = path_label(raw_path) + suffix
        path = PurePosixPath(name)
        if mode not in (b'100644', b'100755'):
            failures.append((label, 'symlink, submodule or unsupported file mode'))
            return
        if (set(path.parts) & PRIVATE_PARTS or path.name == 'config.local.json'
                or path.name == '.env' or path.name.startswith('.env.')
                or path.suffix.lower() in PRIVATE_SUFFIXES):
            failures.append((label, 'private artifact or document type'))
        scan_patterns(label, raw_path)
        content = content_for(object_id)
        if b'\0' in content:
            failures.append((label, 'binary content requires explicit release review'))
        scan_patterns(label, content)

    for entry in filter(None, result.stdout.split(b'\0')):
        metadata, raw_path = entry.split(b'\t', 1)
        mode, object_id, stage = metadata.split()
        count += 1
        if stage != b'0':
            failures.append((path_label(raw_path), 'unresolved merge'))
        scan_file(raw_path, mode, object_id)
    if count == 0:
        print('No staged/tracked files found. Stage the candidate files before checking.', file=sys.stderr)
        return 1
    commits, historical_files = [], set()
    if args.history:
        revisions = ['git', 'rev-list', '--all']
        if subprocess.run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD'], capture_output=True).returncode == 0:
            revisions.append('HEAD')
        commits = subprocess.check_output(revisions).splitlines()
        for commit in commits:
            revision = commit.decode('ascii')
            scan_patterns('commit ' + revision[:12], content_for(commit, 'commit'))
            tree = subprocess.check_output(['git', 'ls-tree', '-rz', '--full-tree', revision])
            for entry in filter(None, tree.split(b'\0')):
                metadata, raw_path = entry.split(b'\t', 1)
                mode, kind, object_id = metadata.split()
                identity = (raw_path, mode, object_id)
                if identity not in historical_files:
                    historical_files.add(identity)
                    scan_file(raw_path, mode, object_id, ' (history ' + revision[:12] + ')')
        # Tags can point directly at blobs outside any commit's file tree.
        objects = subprocess.check_output(revisions[:2] + ['--objects', '--no-object-names'] + revisions[2:])
        inventory = subprocess.run(['git', 'cat-file', '--batch-check=%(objectname) %(objecttype)'],
                                   input=objects, capture_output=True, check=True).stdout
        for line in inventory.splitlines():
            object_id, kind = line.split()
            if kind == b'blob' and ('blob', object_id) not in contents:
                label = 'blob ' + object_id.decode('ascii')[:12]
                content = content_for(object_id)
                if b'\0' in content:
                    failures.append((label, 'binary content requires explicit release review'))
                scan_patterns(label, content)
        refs = subprocess.check_output(['git', 'for-each-ref', '--format=%(objecttype) %(objectname)'])
        seen_tags = set()
        for line in refs.splitlines():
            kind, object_id = line.split()
            while kind == b'tag' and object_id not in seen_tags:
                seen_tags.add(object_id)
                content = content_for(object_id, 'tag')
                scan_patterns('tag ' + object_id.decode('ascii')[:12], content)
                headers = dict(line.split(b' ', 1) for line in content.split(b'\n\n', 1)[0].splitlines())
                kind, object_id = headers[b'type'], headers[b'object']
    for name, reason in dict.fromkeys(failures):
        # Report file and category, never a possible secret value.
        print(f'{name}: {reason}', file=sys.stderr)
    if failures:
        return 1
    history_note = (f' and {len(commits)} reachable commits ({len(historical_files)} historical file versions)'
                    if args.history else '')
    print(f'Public-release pattern checks passed for {count} staged/tracked files{history_note}. '
          'Review names, prose and GitHub metadata separately.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
