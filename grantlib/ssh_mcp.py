"""Launch the shared stdio server using the user's existing SSH credentials.

Some MCP clients intentionally omit SSH_AUTH_SOCK from inherited variables.
On macOS recover the current login-session agent from launchd, avoiding a
hard-coded socket path that becomes invalid after a restart. Never forward
the agent to the remote host or copy a key.
"""
import argparse
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys


def ssh_environment():
    environment = dict(os.environ)
    if environment.get('SSH_AUTH_SOCK') or sys.platform != 'darwin':
        return environment
    try:
        result = subprocess.run(['/bin/launchctl', 'getenv', 'SSH_AUTH_SOCK'],
                                capture_output=True, text=True, timeout=5, check=False)
        candidate = result.stdout.strip()
        if result.returncode == 0 and candidate.startswith('/') and '\n' not in candidate and '\x00' not in candidate:
            info = Path(candidate).stat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid():
                environment['SSH_AUTH_SOCK'] = candidate
    except (OSError, subprocess.TimeoutExpired):
        pass
    return environment


def command(host, remote_python, config):
    if not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._@-]*', host):
        raise ValueError('Use an SSH host alias or hostname, not a shell command.')
    if any(not value.startswith('/') or '\x00' in value or '\n' in value for value in (remote_python, config)):
        raise ValueError('Remote interpreter and config must be absolute paths.')
    remote = shlex.join([remote_python, '-m', 'grantlib.mcp_server', '--config', config])
    # Override forwarding inherited from ssh_config: this connection carries
    # only the stdio MCP session, never an agent, X11 display, or forwarded port.
    return ['/usr/bin/ssh', '-T', '-a', '-x', '-o', 'ClearAllForwardings=yes',
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
            '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3', host, remote]


def main(argv=None):
    parser = argparse.ArgumentParser(description='Connect stdio MCP to the shared grant host through existing SSH authentication')
    parser.add_argument('--host', required=True)
    parser.add_argument('--remote-python', required=True)
    parser.add_argument('--config', required=True, help='Absolute config path on the remote host')
    args = parser.parse_args(argv)
    try:
        arguments = command(args.host, args.remote_python, args.config)
        os.execve(arguments[0], arguments, ssh_environment())
    except (ValueError, OSError) as error:
        parser.exit(1, f'Shared grant connection failed: {error}\n')


if __name__ == '__main__':
    main()
