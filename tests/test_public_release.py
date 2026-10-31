"""Exercise release checks against isolated, invented Git history."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


CHECKER = Path(__file__).resolve().parents[1] / 'scripts' / 'check_public_release.py'


class PublicReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        self.git('init', '-q')
        self.git('config', 'user.name', 'Synthetic contributor')
        self.git('config', 'user.email', 'contributor@example.org')
        self.write('README.md', 'Invented release-check fixture.\n')
        self.commit('Initial synthetic fixture')

    def git(self, *args):
        return subprocess.run(['git', '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=/dev/null', *args],
                              cwd=self.root, env=self.env, check=True, capture_output=True)

    def write(self, name, content):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        self.git('add', name)

    def commit(self, message):
        self.git('commit', '-qm', message)

    def check(self, *args):
        return subprocess.run([sys.executable, str(CHECKER), *args], cwd=self.root, env=self.env,
                              capture_output=True, text=True)

    def test_history_finds_deleted_secret_without_printing_it(self):
        token = 'ghp_' + 'A' * 36
        self.write('obsolete.txt', token)
        self.commit('Add invented secret')
        self.git('rm', '-q', 'obsolete.txt')
        self.commit('Remove invented secret')
        self.assertEqual(self.check().returncode, 0)
        result = self.check('--history')
        self.assertEqual(result.returncode, 1)
        self.assertIn('GitHub token', result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_secret_in_filename_is_redacted(self):
        token = 'ghp_' + 'D' * 36
        self.write(token + '.txt', 'Invented fixture')
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('redacted filename', result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_history_finds_tagged_blob_outside_commit_trees(self):
        token = 'ghp_' + 'E' * 36
        blob = subprocess.run(['git', 'hash-object', '-w', '--stdin'], input=token, text=True,
                              cwd=self.root, env=self.env, capture_output=True, check=True).stdout.strip()
        self.git('update-ref', 'refs/tags/blob-fixture', blob)
        self.assertEqual(self.check().returncode, 0)
        result = self.check('--history')
        self.assertEqual(result.returncode, 1)
        self.assertIn('blob ', result.stderr)
        self.assertNotIn(token, result.stdout + result.stderr)

    def test_history_checks_commit_and_tag_metadata(self):
        path = '/' + 'Users' + '/fictional-person/research'
        self.git('commit', '--allow-empty', '-qm', path)
        self.git('-c', 'tag.gpgsign=false', 'tag', '-a', 'fixture-tag', '-m', path)
        self.git('-c', 'tag.gpgsign=false', 'tag', '-a', 'outer-tag', 'fixture-tag', '-m', 'Clean outer tag')
        self.git('tag', '-d', 'fixture-tag')
        result = self.check('--history')
        self.assertEqual(result.returncode, 1)
        self.assertIn('commit ', result.stderr)
        self.assertIn('tag ', result.stderr)
        self.assertNotIn(path, result.stderr)

    def test_detached_head_history_is_included(self):
        branch = self.git('symbolic-ref', '--short', 'HEAD').stdout.decode().strip()
        self.write('deleted.txt', 'ghp_' + 'C' * 36)
        self.commit('Invented detached history')
        self.git('rm', '-q', 'deleted.txt')
        self.commit('Remove fixture')
        self.git('checkout', '-q', '--detach')
        self.git('branch', '-D', branch)
        self.assertEqual(self.check().returncode, 0)
        result = self.check('--history')
        self.assertEqual(result.returncode, 1)
        self.assertIn('GitHub token', result.stderr)

    def test_rejects_credential_directory_and_submodule(self):
        self.write('.ssh/settings', 'invented settings')
        head = self.git('rev-parse', 'HEAD').stdout.decode().strip()
        self.git('update-index', '--add', '--cacheinfo', '160000,' + head + ',dependency')
        result = self.check()
        self.assertEqual(result.returncode, 1)
        self.assertIn('private artifact', result.stderr)
        self.assertIn('submodule', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_detects_windows_and_encoded_home_paths(self):
        paths = ['C:' + chr(92) + 'Users' + chr(92) + 'fixture' + chr(92) + 'grant.txt',
                 '%2f' + 'Users' + '%2ffixture%2fgrant.txt',
                 chr(92) + 'u002fUsers' + chr(92) + 'u002ffixture' + chr(92) + 'u002fgrant.txt']
        for value in paths:
            with self.subTest(value=value):
                self.write('example.txt', value)
                result = self.check()
                self.assertEqual(result.returncode, 1)
                self.assertNotIn(value, result.stderr)

    def test_full_history_is_required(self):
        shallow = self.root / 'shallow-copy'
        self.git('clone', '-q', '--depth=1', self.root.as_uri(), str(shallow))
        result = subprocess.run([sys.executable, str(CHECKER), '--history'], cwd=shallow,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('complete clone', result.stderr)

    def test_public_links_placeholders_and_ignored_files_are_allowed(self):
        self.write('README.md', 'https://www.ncbi.nlm.nih.gov/home/develop/api/\n'
                   '/absolute/path/to/workspace/config.local.json\n')
        private = self.root / '.local'
        private.mkdir()
        (private / 'untracked.txt').write_text('ghp_' + 'B' * 36)
        self.commit('Update public examples')
        result = self.check('--history')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('2 reachable commits', result.stdout)


if __name__ == '__main__':
    unittest.main()
