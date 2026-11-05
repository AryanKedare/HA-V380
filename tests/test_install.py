"""Installer filesystem tests; downloads are fixtures, with no network access."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

INSTALLER = Path(__file__).resolve().parents[1] / 'install.sh'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='v380-installer-', dir=os.environ.get('V380_TEST_TMPDIR'))
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'HA config with spaces'
        self.config.mkdir()
        self.configuration = 'default_config:\n# Keep this configuration\n'
        (self.config / 'configuration.yaml').write_text(self.configuration)
        (self.config / '.storage').mkdir()
        (self.config / '.storage/core.config_entries').write_text('private-settings-fixture')
        self.source = self.root / 'source checkout'
        self.component = self.source / 'custom_components/v380_cloud'
        self.component.mkdir(parents=True)
        (self.component / 'manifest.json').write_text(json.dumps({'domain': 'v380_cloud', 'version': '0.1.7'}, indent=2))
        for name in ('__init__.py', 'api.py', 'live.py'):
            (self.component / name).write_text('# new component\n')
        (self.component / 'translations').mkdir()
        (self.component / 'translations/en.json').write_text('{"fixture": true}\n')
        (self.component / '__pycache__').mkdir()
        (self.component / '__pycache__/api.cpython-test.pyc').write_bytes(b'cache')
        self.target = self.config / 'custom_components/v380_cloud'
        self.env = {**os.environ, 'HA_CONFIG_DIR': str(self.config)}
        self.bin = self.root / 'bin'
        self.bin.mkdir()

    def run_installer(self, *args, success=True, env=None):
        result = subprocess.run(['sh', str(INSTALLER), *map(str, args)], env=env or self.env,
            capture_output=True, text=True, timeout=15)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.config / 'configuration.yaml').read_text(), self.configuration)
        self.assertEqual((self.config / '.storage/core.config_entries').read_text(), 'private-settings-fixture')
        return result

    def old_install(self):
        self.target.mkdir(parents=True)
        (self.target / 'old.py').write_text('old component\n')
        (self.target / '.hidden').write_text('preserve in backup\n')

    def assert_new_install(self):
        self.assertEqual(json.loads((self.target / 'manifest.json').read_text())['version'], '0.1.7')
        self.assertTrue((self.target / 'translations/en.json').is_file())
        self.assertFalse((self.target / '__pycache__').exists())
        self.assertFalse((self.target.parent / '.v380-cloud-install.lock').exists())
        self.assertFalse(list(self.target.parent.glob('.v380-cloud-stage.*')))

    def mock_command(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)
        return path

    def download_environment(self, downloader):
        archive = self.root / 'fixture.zip'
        with zipfile.ZipFile(archive, 'w') as zipped:
            for path in self.component.rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts:
                    zipped.write(path, 'AryanKedare-HA-V380-fixture/custom_components/v380_cloud/' + str(path.relative_to(self.component)))
        # Isolate PATH so the wget test cannot silently use the host's curl.
        for name in ('sh', 'cat', 'mktemp', 'mkdir', 'cp', 'mv', 'rm', 'rmdir', 'date', 'sed', 'grep', 'unzip', 'find'):
            (self.bin / name).symlink_to(shutil.which(name))
        (self.bin / 'python3').symlink_to(sys.executable)
        self.mock_command(downloader, '''#!/usr/bin/env python3
import os, shutil, sys
args = sys.argv[1:]
option = '--output' if '--output' in args else '-O'
shutil.copyfile(os.environ['V380_TEST_ARCHIVE'], args[args.index(option) + 1])
with open(os.environ['V380_TEST_URL_LOG'], 'w') as output:
    output.write(args[-1])
''')
        return {**self.env, 'PATH': str(self.bin), 'V380_TEST_ARCHIVE': str(archive),
                'V380_TEST_URL_LOG': str(self.root / 'download-url.txt')}

    def test_fresh_offline_install_accepts_paths_with_spaces_and_removes_bytecode(self):
        result = self.run_installer('--source', self.source, '--config-dir', self.config)
        self.assert_new_install()
        self.assertIn('Installed V380 Cloud 0.1.7', result.stdout)
        self.assertFalse((self.config / '.v380-cloud-backups').exists())

    def test_upgrade_preserves_complete_backup_and_other_integrations(self):
        self.old_install()
        other = self.target.parent / 'another_integration'
        other.mkdir()
        (other / 'keep.py').write_text('keep\n')
        self.run_installer('--source', self.component)
        self.assert_new_install()
        backups = list((self.config / '.v380-cloud-backups').glob('*/v380_cloud'))
        self.assertEqual(len(backups), 1)
        self.assertEqual((backups[0] / 'old.py').read_text(), 'old component\n')
        self.assertEqual((backups[0] / '.hidden').read_text(), 'preserve in backup\n')
        self.assertEqual((other / 'keep.py').read_text(), 'keep\n')
        self.assertFalse((self.target / 'old.py').exists())

    def test_failed_replacement_restores_old_component(self):
        self.old_install()
        real_mv = shutil.which('mv')
        self.mock_command('mv', '''#!/bin/sh
if [ "$1" = '--' ]; then shift; fi
case "$1" in */.v380-cloud-stage.*) exit 77 ;; esac
exec ''' + repr(real_mv) + ''' "$@"
''')
        env = {**self.env, 'PATH': str(self.bin) + os.pathsep + self.env['PATH']}
        result = self.run_installer('--source', self.source, success=False, env=env)
        self.assertIn('Restored the previous', result.stderr)
        self.assertEqual((self.target / 'old.py').read_text(), 'old component\n')
        self.assertFalse((self.target / 'manifest.json').exists())
        self.assertFalse((self.target.parent / '.v380-cloud-install.lock').exists())
        self.assertFalse(list(self.target.parent.glob('.v380-cloud-stage.*')))

    def test_missing_config_or_invalid_source_cannot_replace_existing_component(self):
        self.old_install()
        (self.config / 'configuration.yaml').unlink()
        result = subprocess.run(['sh', str(INSTALLER), '--source', str(self.source)],
            env=self.env, capture_output=True, text=True, timeout=15)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('configuration.yaml', result.stderr)
        self.assertEqual((self.target / 'old.py').read_text(), 'old component\n')
        (self.config / 'configuration.yaml').write_text(self.configuration)
        (self.component / 'live.py').unlink()
        self.run_installer('--source', self.source, success=False)
        self.assertEqual((self.target / 'old.py').read_text(), 'old component\n')
        self.assertFalse((self.config / '.v380-cloud-backups').exists())

    def test_existing_installer_lock_is_preserved(self):
        self.old_install()
        lock = self.target.parent / '.v380-cloud-install.lock'
        lock.mkdir()
        self.run_installer('--source', self.source, success=False)
        self.assertTrue(lock.is_dir())
        self.assertEqual((self.target / 'old.py').read_text(), 'old component\n')

    def test_curl_download_installs_pinned_commit(self):
        env = self.download_environment('curl')
        self.run_installer(env=env)
        self.assert_new_install()
        self.assertEqual((self.root / 'download-url.txt').read_text(),
            'https://api.github.com/repos/AryanKedare/HA-V380/zipball/a49c22882c8d38b336ec0262a04490c92ce81ac3')

    def test_wget_fallback_accepts_branch_ref(self):
        env = self.download_environment('wget')
        self.run_installer('--ref', 'release/test-0.1.7', env=env)
        self.assert_new_install()
        self.assertTrue((self.root / 'download-url.txt').read_text().endswith('/zipball/release%2Ftest-0.1.7'))

    def test_failed_download_leaves_existing_component_untouched(self):
        self.old_install()
        self.mock_command('curl', '#!/bin/sh\nexit 22\n')
        env = {**self.env, 'PATH': str(self.bin) + os.pathsep + self.env['PATH']}
        self.run_installer(env=env, success=False)
        self.assertEqual((self.target / 'old.py').read_text(), 'old component\n')
        self.assertFalse((self.config / '.v380-cloud-backups').exists())
