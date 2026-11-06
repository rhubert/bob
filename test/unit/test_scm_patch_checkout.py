# Bob build tool
# SPDX-License-Identifier: GPL-3.0-or-later

"""Exercise overlay recovery through real checkout state and recipe caching."""

from unittest import TestCase, skipUnless
from pathlib import Path
import hashlib
import os
import pickle
import shutil
import subprocess
import sys
import tarfile
import tempfile

import yaml


class TestPatchCheckout(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bobRoot = Path(__file__).resolve().parents[2]
        self.env = {**os.environ, 'PYTHONPATH': str(self.bobRoot / 'pym')}
        (self.root / 'recipes').mkdir()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.git('init')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        for name in ('message', 'second'):
            (self.repo / name).write_text('old\n')
        self.git('add', '.')
        self.git('commit', '-m', 'base')
        self.commit = self.git('rev-parse', 'HEAD').strip()
        self.archive = self.root / 'source.tar'
        with tarfile.open(self.archive, 'w') as archive:
            for name in ('message', 'second'):
                archive.add(self.repo / name, arcname=name)

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', *args], cwd=self.repo,
                                       stderr=subprocess.PIPE, text=True)

    def patch(self, name, before, after, target='message'):
        (self.root / 'recipes' / name).write_text(
            '--- a/{0}\n+++ b/{0}\n@@ -1 +1 @@\n-{1}\n+{2}\n'.format(
                target, before, after))

    def recipe(self, kind, patches, script='true'):
        if kind == 'git':
            scm = {'scm': 'git', 'url': self.repo.as_uri(), 'commit': self.commit}
        else:
            scm = {'scm': 'url', 'url': self.archive.as_uri(),
                   'digestSHA256': hashlib.sha256(self.archive.read_bytes()).hexdigest(),
                   'extract': 'tar'}
        scm.update(dir='src', patches=patches)
        (self.root / 'recipes' / 'root.yaml').write_text(yaml.safe_dump({
            'root': True, 'checkoutSCM': scm, 'checkoutScript': script,
            'checkoutDeterministic': True, 'buildScript': 'true', 'packageScript': 'true',
        }))

    def bob(self, success=True):
        result = subprocess.run([sys.executable, str(self.bobRoot / 'bob'),
                                 '--debug=ngd', 'dev', '--checkout-only', 'root'],
                                cwd=self.root, env=self.env, text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout

    def workspace(self):
        return next((self.root / 'dev' / 'src' / 'root').glob('*/workspace'))

    def test_git_failed_stack_retries_corrected_patch_on_retained_base(self):
        self.check_failed_stack_retry('git')

    def test_url_failed_stack_retries_corrected_patch_on_retained_base(self):
        self.check_failed_stack_retry('url')

    def check_failed_stack_retry(self, kind):
        self.patch('first.patch', 'old', 'first')
        self.patch('second.patch', 'wrong', 'second', 'second')
        self.recipe(kind, [{'file': 'first.patch'}, {'file': 'second.patch'}])
        self.bob(False)
        workspace = self.workspace()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'first\n')
        self.patch('second.patch', 'old', 'second', 'second')
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'first\n')
        self.assertEqual((workspace / 'src' / 'second').read_text(), 'second\n')
        self.assertTrue((workspace.parent / 'attic').exists())
        self.bob()

    def test_unverified_failed_checkout_moves_to_attic(self):
        self.patch('fix.patch', 'wrong', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        self.bob(False)
        workspace = self.workspace()
        (workspace / 'src' / 'message').write_text('partial legacy overlay\n')
        relative = str(workspace.relative_to(self.root))
        subprocess.run([sys.executable, '-c',
                        'from bob.state import BobState, finalize; '
                        'import sys; s=BobState(); d=s.getDirectoryState(sys.argv[1], True); '
                        'd.pop(4, None); s.setDirectoryState(sys.argv[1], d); finalize()',
                        relative], cwd=self.root, env=self.env, check=True)
        self.patch('fix.patch', 'old', 'patched')
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.assertTrue((workspace.parent / 'attic').exists())
        saved = next((workspace.parent / 'attic').glob('*/message'))
        self.assertEqual(saved.read_text(), 'partial legacy overlay\n')

    def test_series_reordering_invalidates_recipe_cache(self):
        self.patch('first.patch', 'old', 'first')
        self.patch('second.patch', 'first', 'second')
        series = self.root / 'recipes' / 'series'
        series.write_text('first.patch\nsecond.patch\n')
        self.recipe('git', [{'series': 'series'}])
        self.bob()
        series.write_text('second.patch\nfirst.patch\n')
        output = self.bob(False)
        self.assertIn('second.patch', output)

    def test_checkout_script_change_with_same_scm_reruns_checkout(self):
        self.recipe('git', [], 'echo first > stamp')
        self.bob()
        workspace = self.workspace()
        self.assertEqual((workspace / 'stamp').read_text(), 'first\n')
        self.recipe('git', [], 'echo second > stamp')
        self.bob()
        self.assertEqual((workspace / 'stamp').read_text(), 'second\n')

    def test_unchanged_git_checkout_keeps_applied_overlay_on_script_change(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}], 'echo first > stamp')
        self.bob()
        workspace = self.workspace()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.recipe('git', [{'file': 'fix.patch'}], 'echo second > stamp')
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.assertEqual((workspace / 'stamp').read_text(), 'second\n')

    def test_unchanged_url_checkout_reapplies_overlay_on_script_change(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('url', [{'file': 'fix.patch'}], 'echo first > stamp')
        self.bob()
        workspace = self.workspace()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.recipe('url', [{'file': 'fix.patch'}], 'echo second > stamp')
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.assertEqual((workspace / 'stamp').read_text(), 'second\n')

    def test_import_checkout_reapplies_overlay_after_source_update(self):
        source = self.root / 'import-source'
        source.mkdir()
        (source / 'message').write_text('old\n')
        (source / 'second').write_text('old\n')
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        recipe = self.root / 'recipes' / 'root.yaml'
        config = yaml.safe_load(recipe.read_text())
        config['checkoutSCM'] = {'scm': 'import', 'url': source.as_posix(), 'dir': 'src',
                                 'patches': [{'file': 'fix.patch'}]}
        config['checkoutDeterministic'] = False
        recipe.write_text(yaml.safe_dump(config))
        self.bob()
        workspace = self.workspace()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        (source / 'second').write_text('updated\n')
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.assertEqual((workspace / 'src' / 'second').read_text(), 'updated\n')

    @skipUnless(shutil.which('svn') and shutil.which('svnadmin'), 'requires Subversion')
    def test_svn_checkout_reapplies_overlay_after_source_update(self):
        source = self.root / 'svn-source'
        source.mkdir()
        (source / 'message').write_text('old\n')
        (source / 'second').write_text('old\n')
        repo = self.root / 'svn-repo'
        subprocess.run(['svnadmin', 'create', str(repo)], check=True, stdout=subprocess.DEVNULL)
        url = repo.as_uri() + '/trunk'
        subprocess.run(['svn', 'import', str(source), url, '-m', 'initial'], check=True,
                       stdout=subprocess.DEVNULL)
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        recipe = self.root / 'recipes' / 'root.yaml'
        config = yaml.safe_load(recipe.read_text())
        config['checkoutSCM'] = {'scm': 'svn', 'url': url, 'dir': 'src',
                                 'patches': [{'file': 'fix.patch'}]}
        config['checkoutDeterministic'] = False
        recipe.write_text(yaml.safe_dump(config))
        self.bob()
        workspace = self.workspace()
        checkout = self.root / 'svn-update'
        subprocess.run(['svn', 'checkout', url, str(checkout)], check=True,
                       stdout=subprocess.DEVNULL)
        (checkout / 'second').write_text('updated\n')
        subprocess.run(['svn', 'commit', '-m', 'update', str(checkout)], check=True,
                       stdout=subprocess.DEVNULL)
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.assertEqual((workspace / 'src' / 'second').read_text(), 'updated\n')

    @skipUnless(os.name == 'posix' and shutil.which('cvs'), 'requires POSIX CVS')
    def test_cvs_checkout_reapplies_overlay_after_source_update(self):
        cvsroot = self.root / 'cvs-repo'
        cvsroot.mkdir()
        source = self.root / 'cvs-source'
        source.mkdir()
        (source / 'message').write_text('old\n')
        (source / 'second').write_text('old\n')
        subprocess.run(['cvs', '-d', str(cvsroot), 'init'], check=True,
                       stdout=subprocess.DEVNULL)
        subprocess.run(['cvs', '-d', str(cvsroot), 'import', '-m', 'initial',
                        'module', 'vendor', 'start'], cwd=source, check=True,
                       stdout=subprocess.DEVNULL)
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        recipe = self.root / 'recipes' / 'root.yaml'
        config = yaml.safe_load(recipe.read_text())
        config['checkoutSCM'] = {'scm': 'cvs', 'cvsroot': str(cvsroot), 'module': 'module',
                                 'dir': 'src', 'patches': [{'file': 'fix.patch'}]}
        config['checkoutDeterministic'] = False
        recipe.write_text(yaml.safe_dump(config))
        self.bob()
        workspace = self.workspace()
        checkout = self.root / 'cvs-update'
        subprocess.run(['cvs', '-q', '-d', str(cvsroot), 'checkout', '-d', str(checkout),
                        'module'], cwd=self.root, check=True, stdout=subprocess.DEVNULL)
        (checkout / 'second').write_text('updated\n')
        subprocess.run(['cvs', '-q', 'commit', '-m', 'update', 'second'], cwd=checkout,
                       check=True, stdout=subprocess.DEVNULL)
        self.bob()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        self.assertEqual((workspace / 'src' / 'second').read_text(), 'updated\n')

    def test_changed_patched_tree_is_not_silently_recorded_as_managed(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}], 'echo first > stamp')
        self.bob()
        workspace = self.workspace()
        message = workspace / 'src' / 'message'
        message.write_text('user modification\n')
        self.recipe('git', [{'file': 'fix.patch'}], 'echo second > stamp')
        output = self.bob(False)
        self.assertIn('Patched checkout was modified', output)
        self.assertEqual(message.read_text(), 'user modification\n')

    def test_tracking_git_branch_only_repatches_when_upstream_changes(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        recipe = self.root / 'recipes' / 'root.yaml'
        config = yaml.safe_load(recipe.read_text())
        del config['checkoutSCM']['commit']
        config['checkoutSCM']['branch'] = self.git('branch', '--show-current').strip()
        config['checkoutDeterministic'] = False
        recipe.write_text(yaml.safe_dump(config))
        self.bob()
        workspace = self.workspace()
        patched = workspace / 'src' / 'message'
        before = patched.stat().st_mtime_ns
        self.bob()
        self.assertEqual(patched.stat().st_mtime_ns, before)
        (self.repo / 'second').write_text('upstream\n')
        self.git('add', 'second')
        self.git('commit', '-m', 'update')
        self.bob()
        self.assertEqual(patched.read_text(), 'patched\n')
        self.assertEqual((workspace / 'src' / 'second').read_text(), 'upstream\n')

    def test_moving_git_ref_only_repatches_when_upstream_changes(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        recipe = self.root / 'recipes' / 'root.yaml'
        config = yaml.safe_load(recipe.read_text())
        del config['checkoutSCM']['commit']
        self.git('update-ref', 'refs/bob/overlay-test', self.commit)
        config['checkoutSCM'].update(rev='refs/bob/overlay-test', rebase=True)
        config['checkoutDeterministic'] = False
        recipe.write_text(yaml.safe_dump(config))
        self.bob()
        workspace = self.workspace()
        patched = workspace / 'src' / 'message'
        before = patched.stat().st_mtime_ns
        self.bob()
        self.assertEqual(patched.stat().st_mtime_ns, before)
        (self.repo / 'second').write_text('upstream\n')
        self.git('add', 'second')
        self.git('commit', '-m', 'update')
        self.git('update-ref', 'refs/bob/overlay-test', 'HEAD')
        self.bob()
        self.assertEqual(patched.read_text(), 'patched\n')
        self.assertEqual((workspace / 'src' / 'second').read_text(), 'upstream\n')

    def test_checkout_script_failure_keeps_successful_overlay_state(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}], 'exit 1')
        output = self.bob(False)
        workspace = self.workspace()
        self.assertTrue((workspace / 'src' / 'message').exists(), output)
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'patched\n')
        relative = str(workspace.relative_to(self.root))
        output = subprocess.check_output([sys.executable, '-c',
            'from bob.state import BobState, finalize; import sys; '
            's=BobState(); d=s.getDirectoryState(sys.argv[1], True); '
            'print(bool(d.get(2)), bool(d.get(3))); finalize()', relative],
            cwd=self.root, env=self.env, text=True)
        self.assertEqual(output.strip(), 'True False')

    def interruptNextPatch(self):
        realPatch = shutil.which('patch')
        self.assertIsNotNone(realPatch)
        fakeBin = self.root / 'fake-bin'
        fakeBin.mkdir()
        fakePatch = fakeBin / 'patch'
        fakePatch.write_text(
            '#!{}\n'.format(sys.executable) +
            'import os, signal, sys\n'
            'if "--version" in sys.argv or "--dry-run" in sys.argv:\n'
            '    os.execv({!r}, [{!r}] + sys.argv[1:])\n'.format(realPatch, realPatch) +
            'with open("message", "w") as f: f.write("partial\\n")\n'
            'os.kill(os.getppid(), signal.SIGKILL)\n')
        fakePatch.chmod(0o755)
        self.env['PATH'] = str(fakeBin) + os.pathsep + self.env['PATH']
        result = subprocess.run([sys.executable, str(self.bobRoot / 'bob'),
                                 '--debug=ngd', 'dev', '--checkout-only', 'root'],
                                cwd=self.root, env=self.env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        workspace = self.workspace()
        self.assertEqual((workspace / 'src' / 'message').read_text(), 'partial\n')
        with open(self.root / '.bob-state.pickle.new', 'rb') as f:
            state = pickle.load(f)
        checkout = state['dirStates'][str(workspace.relative_to(self.root))]
        self.assertIn('src', checkout[3])

    @skipUnless(os.name == 'posix', 'requires POSIX process signals')
    def test_interruption_during_patch_persists_recovery_checkpoint(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        self.interruptNextPatch()

    @skipUnless(os.name == 'posix', 'requires POSIX process signals')
    def test_interruption_during_inline_reverse_persists_recovery_checkpoint(self):
        self.patch('fix.patch', 'old', 'patched')
        self.recipe('git', [{'file': 'fix.patch'}])
        self.bob()
        self.patch('fix.patch', 'old', 'corrected')
        self.interruptNextPatch()
