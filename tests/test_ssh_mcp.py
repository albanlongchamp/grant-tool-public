"""SSH environment mocks and offline OpenSSH configuration checks; no connections."""
import os
from pathlib import Path
import shlex
import stat
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from grantlib import ssh_mcp


class SSHLauncherTests(unittest.TestCase):
    def test_recovers_current_macos_agent_when_client_filters_environment(self):
        result = SimpleNamespace(returncode=0, stdout='/fictional/login-agent\n')
        info = SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid())
        with patch.dict(os.environ, {'HOME': '/fictional'}, clear=True), \
             patch.object(ssh_mcp.sys, 'platform', 'darwin'), \
             patch.object(ssh_mcp.subprocess, 'run', return_value=result) as run, \
             patch.object(ssh_mcp.Path, 'stat', return_value=info):
            environment = ssh_mcp.ssh_environment()
        self.assertEqual(environment['SSH_AUTH_SOCK'], '/fictional/login-agent')
        self.assertEqual(run.call_args.args[0], ['/bin/launchctl', 'getenv', 'SSH_AUTH_SOCK'])

    def test_existing_agent_environment_is_preserved_without_lookup(self):
        with patch.dict(os.environ, {'SSH_AUTH_SOCK': '/fictional/existing'}, clear=True), \
             patch.object(ssh_mcp.subprocess, 'run') as run:
            self.assertEqual(ssh_mcp.ssh_environment()['SSH_AUTH_SOCK'], '/fictional/existing')
            run.assert_not_called()

    def test_missing_or_foreign_agent_is_not_guessed(self):
        result = SimpleNamespace(returncode=0, stdout='/fictional/other\n')
        with patch.dict(os.environ, {}, clear=True), patch.object(ssh_mcp.sys, 'platform', 'darwin'), \
             patch.object(ssh_mcp.subprocess, 'run', return_value=result), \
             patch.object(ssh_mcp.Path, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.getuid()+1)):
            self.assertNotIn('SSH_AUTH_SOCK', ssh_mcp.ssh_environment())

    def test_remote_paths_are_shell_quoted(self):
        interpreter = '/fictional/with spaces/python'
        config = '/fictional/$(not-a-command)/config.json'
        command = ssh_mcp.command('my-host', interpreter, config)
        self.assertEqual(shlex.split(command[-1]), [interpreter, '-m', 'grantlib.mcp_server', '--config', config])
        self.assertNotIn('-A', command)
        self.assertIn('BatchMode=yes', command)
        with self.assertRaises(ValueError):
            ssh_mcp.command('-oProxyCommand=bad', interpreter, config)

    @unittest.skipUnless(Path('/usr/bin/ssh').is_file(), 'OpenSSH is unavailable')
    def test_command_disables_forwarding_in_resolved_ssh_configuration(self):
        # -G prints configuration and exits without connecting. -F selects only
        # this invented fixture, not the user's SSH configuration or credentials.
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / 'ssh_config'
            fixture.write_text('''Host my-host
    HostName example.invalid
    CanonicalizeHostname no
    ForwardAgent yes
    ForwardX11 yes
    LocalForward 127.0.0.1:49152 example.invalid:443
    RemoteForward 127.0.0.1:49153 example.invalid:443
''')

            def resolved(arguments):
                result = subprocess.run(['/usr/bin/ssh', '-G', '-F', str(fixture), *arguments],
                                        check=True, capture_output=True, text=True, timeout=5)
                return dict(line.split(None, 1) for line in result.stdout.splitlines() if line.strip())

            inherited = resolved(['-T', 'my-host'])
            self.assertEqual(inherited['forwardagent'], 'yes')
            self.assertEqual(inherited['forwardx11'], 'yes')
            self.assertIn('localforward', inherited)
            self.assertIn('remoteforward', inherited)

            command = ssh_mcp.command('my-host', '/fictional/python', '/fictional/config.json')
            actual = resolved(command[1:])
            self.assertEqual(actual['forwardagent'], 'no')
            self.assertEqual(actual['forwardx11'], 'no')
            self.assertEqual(actual['clearallforwardings'], 'yes')
            self.assertNotIn('localforward', actual)
            self.assertNotIn('remoteforward', actual)


if __name__ == '__main__':
    unittest.main()
