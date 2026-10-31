"""Headless maintenance commands and per-user macOS launchd setup."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys

from . import refresh
from .locking import BusyError


def register(sub):
    command = sub.add_parser('refresh', help='Refresh configured public collections (no model calls)')
    command.add_argument('--layers', nargs='+', choices=refresh.PUBLIC_LAYERS, default=list(refresh.PUBLIC_LAYERS))
    command.add_argument('--due', action='store_true', help='Skip layers successfully refreshed within seven days')
    sub.add_parser('refresh-status', help='Show last successful refresh, partial coverage and errors')
    watch = sub.add_parser('refresh-watch', help='One debounced archive scan with daily content-hash backstop')
    watch.add_argument('--force', action='store_true', help='Rebuild now; still reject changes during the build')
    install = sub.add_parser('refresh-install', help='Install launchd maintenance on this canonical Mac')
    install.add_argument('--load', action='store_true', help='Also bootstrap the generated jobs into launchd')


def install(config, config_path, *, load=False, home=None, interpreter=None):
    if sys.platform != 'darwin':
        raise ValueError('Automatic scheduling currently supports macOS launchd only.')
    if config.get('refresh', {}).get('enabled') is not True:
        raise ValueError('Enable refresh only on the chosen canonical host before installing jobs.')
    home = Path(home or Path.home())
    target = home / 'Library/LaunchAgents'
    target.mkdir(parents=True, exist_ok=True)
    logs = Path(config['data_dir']) / 'refresh/logs'
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    command = [str(Path(interpreter or sys.executable).absolute()), '-m', 'grantlib.refresh_worker',
               '--config', str(Path(config_path).resolve())]
    results = []
    for kind in ('public', 'archive'):
        label = 'org.grantlib.refresh.' + kind
        plist = {'Label': label, 'ProgramArguments': command + ['--kind', kind],
                 'RunAtLoad': True, 'ProcessType': 'Background', 'Umask': 0o077,
                 'EnvironmentVariables': {'PATH': '/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin'},
                 'StandardOutPath': str(logs / (kind + '.out.log')),
                 'StandardErrorPath': str(logs / (kind + '.err.log'))}
        if kind == 'archive':
            plist['StartInterval'] = 300
        else:
            # A daily check catches downtime/retries; successful collections run weekly.
            plist['StartCalendarInterval'] = {'Hour': 3, 'Minute': 15}
        path = target / (label + '.plist')
        path.write_bytes(plistlib.dumps(plist))
        os.chmod(path, 0o600)
        if load:
            domain = f'gui/{os.getuid()}'
            subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True)
            subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(path)], check=True, capture_output=True)
        results.append({'label': label, 'path': str(path), 'loaded': load})
    return {'jobs': results, 'public': 'Daily at 03:15 host local time: refresh each layer when seven days old; retry failed/partial layers.',
            'archive': 'Scan every five minutes; wait for stable inputs; full content-hash scan daily.',
            'availability': 'Per-user launch agents require the user session and mounted source folders; missed work runs after startup.'}


def dispatch(args, config):
    try:
        if args.command == 'refresh-status':
            result = refresh.status(config['data_dir'])
        elif args.command == 'refresh-watch':
            result = refresh.watch(config, force=args.force)
        elif args.command == 'refresh-install':
            result = install(config, args.config, load=args.load)
        else:
            result = refresh.run(config, args.layers, due=args.due)
    except BusyError as error:
        result = {'status': 'busy', 'message': str(error)}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if result.get('status') == 'failed' or any(isinstance(v, dict) and v.get('status') in ('failed', 'partial') for v in result.values()):
        return 1
    return 0
