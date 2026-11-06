# Bob build tool
# SPDX-License-Identifier: GPL-3.0-or-later

"""Regression coverage for managed, recipe-local SCM patch overlays."""

from unittest import TestCase, skipUnless
from unittest.mock import patch, AsyncMock
import asyncio
import os
import subprocess
import tempfile

from bob.errors import ParseError
from bob.scm.patch import PatchScm, PatchApplyError
from bob.scm.scm import Scm, ScmStatus, ScmTaint, overlayFingerprint
from bob.scm.url import UrlScm
from bob.utils import hashDirectory, canSymlink


class DummyScm(Scm):
    def __init__(self, recipe, directory='src', protected=()):
        self.recipe = recipe
        self.directory = directory
        self.protected = protected

    def _getRecipe(self): return self.recipe
    def getSource(self): return 'dummy source'
    def getDirectory(self): return self.directory
    def getProtectedPaths(self): return self.protected
    async def invoke(self, invoker, workspaceCreated): pass
    async def switch(self, invoker, oldScm): return True
    def canSwitch(self, oldScm): return isinstance(oldScm, DummyScm)
    def getProperties(self, isJenkins, pretty=False):
        return {'scm': 'dummy', 'recipe': self.recipe, 'dir': self.directory}
    def hasJenkinsPlugin(self): return False
    def asDigestScript(self): return 'dummy'
    def status(self, workspace): return ScmStatus(ScmTaint.modified)
    def isDeterministic(self): return True
    def isLocal(self): return False
    def hasLiveBuildId(self): return False
    async def predictLiveBuildId(self, step): return None
    def calcLiveBuildId(self, workspace): return None
    def getAuditSpec(self): return None
    def getActiveOverrides(self): return set()
    def postAttic(self, workspace): pass


class PluginScm(DummyScm):
    def __init__(self, recipe):
        super().__init__(recipe)
        self.invoked = False

    async def invoke(self, invoker, workspaceCreated): self.invoked = True
    def hasJenkinsPlugin(self): return True
    def asJenkins(self, workPath, config): return 'plugin'


class Invoker:
    def __init__(self, workspace):
        self.workspace = workspace
        self.infoMessages = []

    def joinPath(self, *parts): return os.path.join(self.workspace, *parts)

    def info(self, *args): self.infoMessages.append(' '.join(map(str, args)))

    async def checkCommand(self, args, cwd=None):
        subprocess.run(args, cwd=self.joinPath(cwd), check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)


class TestPatchScm(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.recipe = os.path.join(self.temp.name, 'recipe.yaml')
        self.workspace = os.path.join(self.temp.name, 'workspace')
        os.makedirs(os.path.join(self.workspace, 'src'))
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('old\n')

    def tearDown(self):
        self.temp.cleanup()

    def writePatch(self, body, newline=None):
        with open(os.path.join(self.temp.name, 'change.patch'), 'w', newline=newline) as f:
            f.write(body)

    def scm(self, patches, overlay=None):
        return PatchScm(DummyScm(self.recipe), patches, overlay)

    def test_changed_patch_reverses_persisted_old_contents(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        old = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(old.invoke(invoker, False))
        properties = old.getProperties(False)

        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+second\n')
        new = self.scm([{'file': 'change.patch'}])
        persisted = self.scm(properties['patches'], {
            'fingerprint': hashDirectory(os.path.join(self.workspace, 'src')).hex()})
        checkpoints = []
        def checkpoint():
            with open(os.path.join(self.workspace, 'src', 'message')) as f:
                checkpoints.append(f.read())
        persisted.setCheckoutProgressCallback(checkpoint)
        asyncio.run(new.switch(invoker, persisted))
        self.assertEqual(checkpoints, ['first\n'])
        asyncio.run(new.invoke(invoker, False))

        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'second\n')

    def test_changed_strip_reverses_persisted_contents_with_new_strip(self):
        os.makedirs(os.path.join(self.workspace, 'src', 'workspace'))
        with open(os.path.join(self.workspace, 'src', 'setupinfo.py'), 'w') as f:
            f.write('old\n')
        self.writePatch('--- workspace/setupinfo.py.orig\n+++ workspace/setupinfo.py\n'
                        '@@ -1 +1 @@\n-old\n+new\n')
        invoker = Invoker(self.workspace)
        # This represents a checkout state written by an earlier overlay
        # configuration.  Its contents are patched, but its strip was wrong.
        with open(os.path.join(self.workspace, 'src', 'setupinfo.py'), 'w') as f:
            f.write('new\n')
        old = self.scm([{'file': 'change.patch', 'strip': 0}], {
            'fingerprint': hashDirectory(os.path.join(self.workspace, 'src')).hex()})
        new = self.scm([{'file': 'change.patch', 'strip': 1}])

        asyncio.run(new.switch(invoker, old))
        asyncio.run(new.invoke(invoker, False))

        with open(os.path.join(self.workspace, 'src', 'setupinfo.py')) as f:
            self.assertEqual(f.read(), 'new\n')

    def test_failed_old_overlay_is_not_reversed_before_applying_fix(self):
        self.writePatch('--- a/missing\n+++ b/missing\n@@ -1 +1 @@\n-old\n+bad\n')
        failed = PatchScm(DummyScm(self.recipe), [{'file': 'change.patch'}],
                          overlayFailed=True,
                          failedBase=overlayFingerprint(os.path.join(self.workspace, 'src')))
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+fixed\n')
        new = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)

        asyncio.run(new.switch(invoker, failed))
        asyncio.run(new.invoke(invoker, False))

        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'fixed\n')

    def test_unmanaged_old_overlay_is_not_reversed_before_applying_fix(self):
        self.writePatch('--- a/missing\n+++ b/missing\n@@ -1 +1 @@\n-old\n+bad\n')
        # Older checkout state has patch bytes but no overlay fingerprint, so
        # it cannot establish that this overlay was ever applied.
        old = self.scm([{'file': 'change.patch'}])
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+fixed\n')
        new = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)

        asyncio.run(new.switch(invoker, old))
        asyncio.run(new.invoke(invoker, False))

        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'fixed\n')

    def test_delegates_source_for_checkout_error_reporting(self):
        self.assertEqual(self.scm([]).getSource(), 'dummy source')

    def test_checkout_identity_identifies_gnu_patch_semantics(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        digest = self.scm([{'file': 'change.patch'}]).asDigestScript()
        self.assertIn('patch-format gnu-patch-v1', digest)

    def test_rejects_non_gnu_patch_executable(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b'other patch implementation\n', None)
        with patch('bob.scm.patch.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=process)):
            with self.assertRaisesRegex(ParseError, 'GNU Patch is required'):
                asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
                    Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'old\n')

    def test_reports_missing_gnu_patch_executable(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        with patch('bob.scm.patch.asyncio.create_subprocess_exec',
                   new=AsyncMock(side_effect=FileNotFoundError('patch'))):
            with self.assertRaisesRegex(ParseError, 'cannot run GNU Patch'):
                asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
                    Invoker(self.workspace), False))

    def test_applies_and_reverses_in_workspace_with_spaces(self):
        workspace = os.path.join(self.workspace, 'workspace with spaces')
        os.makedirs(os.path.join(workspace, 'src'))
        target = os.path.join(workspace, 'src', 'message')
        with open(target, 'wb') as f:
            f.write(b'old\n')
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n', newline='\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'new\n')
        asyncio.run(scm._PatchScm__unapply(invoker))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'old\n')

    def test_checkout_progress_is_checkpointed_before_patch_writes(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        scm = self.scm([{'file': 'change.patch'}])
        observed = []
        def checkpoint():
            with open(os.path.join(self.workspace, 'src', 'message')) as f:
                observed.append(f.read())
        scm.setCheckoutProgressCallback(checkpoint)
        asyncio.run(scm.invoke(Invoker(self.workspace), False))
        self.assertEqual(observed, ['old\n'])
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'new\n')

    def test_legacy_failed_overlay_cannot_switch_without_verified_base(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        failed = PatchScm(DummyScm(self.recipe), [{'file': 'change.patch'}],
                          overlayFailed=True)
        new = self.scm([{'file': 'change.patch'}])
        self.assertFalse(new.canSwitch(failed))
        with self.assertRaisesRegex(ParseError, 'verified unchanged base'):
            asyncio.run(new.switch(Invoker(self.workspace), failed))

    def test_legacy_direct_fingerprint_recovery_state_remains_readable(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        scm = self.scm([{'file': 'change.patch'}])
        path = os.path.join(self.workspace, 'src')
        scm.setCheckoutFailureState(overlayFingerprint(path))
        self.assertTrue(scm.canRetryCheckout(path))

    def test_modified_failed_base_is_not_reused(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        failed = PatchScm(DummyScm(self.recipe), [{'file': 'change.patch'}],
                          overlayFailed=True,
                          failedBase=overlayFingerprint(os.path.join(self.workspace, 'src')))
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('user modification\n')
        with self.assertRaisesRegex(ParseError, 'verified unchanged base'):
            asyncio.run(self.scm([{'file': 'change.patch'}]).switch(
                Invoker(self.workspace), failed))

    def test_failure_in_later_file_leaves_complete_tree_unchanged(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n'
                        '--- a/missing\n+++ b/missing\n@@ -1 +1 @@\n-old\n+new\n')
        before = overlayFingerprint(os.path.join(self.workspace, 'src'))
        with self.assertRaises(PatchApplyError) as error:
            asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
                Invoker(self.workspace), False))
        self.assertEqual(error.exception.baseFingerprint, before)
        self.assertEqual(overlayFingerprint(os.path.join(self.workspace, 'src')), before)

    def test_failure_in_later_patch_leaves_partial_stack_for_inspection(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        with open(os.path.join(self.temp.name, 'bad.patch'), 'w') as f:
            f.write('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-wrong\n+bad\n')
        with self.assertRaises(PatchApplyError) as error:
            asyncio.run(self.scm([{'file': 'change.patch'}, {'file': 'bad.patch'}]).invoke(
                Invoker(self.workspace), False))
        self.assertIsNone(error.exception.baseFingerprint)
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'new\n')

    def test_failed_real_patch_does_not_claim_verified_base(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        version = AsyncMock(returncode=0)
        version.communicate.return_value = (b'GNU patch 2.8\n', None)
        dryRun = AsyncMock(returncode=0)
        dryRun.communicate.return_value = (b'checking file message\n', None)
        actual = AsyncMock(returncode=1)
        async def partial(data):
            with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
                f.write('partially changed\n')
            return (b'write failed\n', None)
        actual.communicate.side_effect = partial
        with patch('bob.scm.patch.asyncio.create_subprocess_exec',
                   new=AsyncMock(side_effect=[version, dryRun, actual])):
            with self.assertRaises(PatchApplyError) as error:
                asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
                    Invoker(self.workspace), False))
        self.assertIsNone(error.exception.baseFingerprint)
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'partially changed\n')

    def test_failed_real_reverse_retains_partial_checkout(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        target = os.path.join(self.workspace, 'src', 'message')
        with open(target, 'w') as f:
            f.write('new\n')
        version = AsyncMock(returncode=0)
        version.communicate.return_value = (b'GNU patch 2.8\n', None)
        dryRun = AsyncMock(returncode=0)
        dryRun.communicate.return_value = (b'checking file message\n', None)
        actual = AsyncMock(returncode=1)
        async def partial(data):
            with open(target, 'w') as f:
                f.write('partial reverse\n')
            return (b'reversal failed\n', None)
        actual.communicate.side_effect = partial
        scm = self.scm([{'file': 'change.patch'}])
        checkpoints = []
        scm.setCheckoutProgressCallback(lambda: checkpoints.append(True))
        with patch('bob.scm.patch.asyncio.create_subprocess_exec',
                   new=AsyncMock(side_effect=[version, dryRun, actual])):
            with self.assertRaisesRegex(ParseError, 'reversal failed'):
                asyncio.run(scm._PatchScm__unapply(Invoker(self.workspace)))
        self.assertEqual(checkpoints, [True])
        with open(target) as f:
            self.assertEqual(f.read(), 'partial reverse\n')

    def test_creation_and_reverse_creation_refuse_existing_target(self):
        for body, reverse in (
                ('--- /dev/null\n+++ b/message\n@@ -0,0 +1 @@\n+new\n', False),
                ('--- a/message\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n', True)):
            self.writePatch(body)
            scm = self.scm([{'file': 'change.patch'}])
            with self.assertRaisesRegex(ParseError, 'already exists'):
                if reverse:
                    asyncio.run(scm._PatchScm__unapply(Invoker(self.workspace)))
                else:
                    asyncio.run(scm.invoke(Invoker(self.workspace), False))
            with open(os.path.join(self.workspace, 'src', 'message')) as f:
                self.assertEqual(f.read(), 'old\n')

    @skipUnless(canSymlink(), 'symlinks unavailable')
    def test_protected_target_normalization_and_symlink_aliases(self):
        for target in ('./message', 'alias', 'nested/message'):
            if target == 'alias':
                os.symlink('message', os.path.join(self.workspace, 'src', 'alias'))
            elif target.startswith('nested/'):
                os.symlink('.', os.path.join(self.workspace, 'src', 'nested'))
            self.writePatch('--- a/{0}\n+++ b/{0}\n@@ -1 +1 @@\n-old\n+new\n'.format(target))
            scm = PatchScm(DummyScm(self.recipe, protected=('message',)),
                           [{'file': 'change.patch'}])
            with self.assertRaisesRegex(ParseError, 'protected SCM path'):
                asyncio.run(scm.invoke(Invoker(self.workspace), False))

    @skipUnless(canSymlink(), 'symlinks unavailable')
    def test_creation_refuses_dangling_symlink(self):
        os.symlink('absent', os.path.join(self.workspace, 'src', 'link'))
        self.writePatch('--- /dev/null\n+++ b/link\n@@ -0,0 +1 @@\n+new\n')
        with self.assertRaises(ParseError):
            asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
                Invoker(self.workspace), False))
        self.assertTrue(os.path.islink(os.path.join(self.workspace, 'src', 'link')))
        self.assertFalse(os.path.exists(os.path.join(self.workspace, 'src', 'absent')))

    def test_nonzero_empty_hunk_coordinates_apply_and_reverse(self):
        self.writePatch('--- a/message\n+++ b/message\n'
                        '@@ -1,0 +2 @@\n+inserted\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'old\ninserted\n')
        asyncio.run(scm._PatchScm__unapply(invoker))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'old\n')

    def test_removed_content_may_look_like_file_header(self):
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('-- text\n')
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n--- text\n+new\n')
        asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'new\n')

    def test_series_content_is_registered_with_cache(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        with open(os.path.join(self.temp.name, 'series'), 'w') as f:
            f.write('change.patch\n')
        registered = []
        def load(path):
            registered.append(path)
            with open(path, 'rb') as f:
                return f.read()
        PatchScm.trackPatchFiles(self.recipe, [{'series': 'series'}], load)
        self.assertEqual(registered, [os.path.join(self.temp.name, 'series'),
                                      os.path.join(self.temp.name, 'change.patch')])

    @skipUnless(canSymlink(), 'symlinks unavailable')
    def test_rejects_patch_and_series_symlinks_outside_recipe(self):
        with tempfile.TemporaryDirectory() as outside:
            for name, contents in (('external.patch', 'patch data'),
                                   ('series', 'external.patch\n')):
                with open(os.path.join(outside, name), 'w') as f:
                    f.write(contents)
                os.symlink(os.path.join(outside, name), os.path.join(self.temp.name, name))
            for spec in ({'file': 'external.patch'}, {'files': 'external*.patch'},
                         {'series': 'series'}):
                with self.assertRaisesRegex(ParseError, 'escapes recipe directory'):
                    self.scm([spec])
                with self.assertRaisesRegex(ParseError, 'escapes recipe directory'):
                    PatchScm.trackPatchFiles(self.recipe, [spec], lambda path: b'')

    def test_variant_comparison_keeps_recipe_identity_and_ignores_metadata(self):
        from bob.builder import compareDirectoryState
        original = {None: (b'old', None), 'src': ('same', {})}
        changed = {None: (b'new', None), 'src': ('same', {})}
        self.assertFalse(compareDirectoryState(original, changed))
        self.assertTrue(compareDirectoryState(original, {**original, 2: {}, 3: [], 4: {}}))

    def test_series_applies_in_order_and_rejects_options(self):
        for name, before, after in (('first.patch', 'old', 'first'),
                                    ('second.patch', 'first', 'second')):
            with open(os.path.join(self.temp.name, name), 'w') as f:
                f.write('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-{}\n+{}\n'
                        .format(before, after))
        with open(os.path.join(self.temp.name, 'series'), 'w') as f:
            f.write('# vendor fixes\n\nfirst.patch\nsecond.patch\n')
        asyncio.run(self.scm([{'series': 'series'}]).invoke(Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'second\n')

        with open(os.path.join(self.temp.name, 'series'), 'w') as f:
            f.write('first.patch -p1\n')
        with self.assertRaises(ParseError):
            self.scm([{'series': 'series'}])

    def test_files_glob_applies_sorted_matches_and_rejects_empty_patterns(self):
        os.makedirs(os.path.join(self.temp.name, 'patches'))
        for name, before, after in (('foo-20.patch', 'first', 'second'),
                                    ('foo-10.patch', 'old', 'first')):
            with open(os.path.join(self.temp.name, 'patches', name), 'w') as f:
                f.write('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-{}\n+{}\n'
                        .format(before, after))

        scm = self.scm([{'files': 'patches/foo*.patch'}])
        asyncio.run(scm.invoke(Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'second\n')
        self.assertEqual([patch['file'] for patch in scm.getProperties(False)['patches']],
                         [os.path.join('patches', 'foo-10.patch'),
                          os.path.join('patches', 'foo-20.patch')])

        for pattern in ('patches/missing*.patch', '../patches/*.patch', '**/*.patch'):
            with self.assertRaises(ParseError):
                self.scm([{'files': pattern}])

    def test_rejects_negative_patch_strip_level(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        with self.assertRaises(ParseError):
            self.scm([{'file': 'change.patch', 'strip': -1}])

    def test_registers_patch_contents_with_recipe_cache(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        class RecipeCache:
            def __init__(self): self.paths = []
            def loadBinary(self, path):
                self.paths.append(path)
                with open(path, 'rb') as f:
                    return f.read()
        cache = RecipeCache()
        PatchScm(DummyScm(self.recipe), [{'file': 'change.patch'}], recipeSet=cache)
        self.assertEqual(cache.paths, [os.path.join(self.temp.name, 'change.patch')])
        cache.paths.clear()
        PatchScm.trackPatchFiles(self.recipe, [{'file': 'change.patch'}], cache.loadBinary)
        self.assertEqual(cache.paths, [os.path.join(self.temp.name, 'change.patch')])

    def test_accepts_execution_recipeset_without_cache_api(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        PatchScm(DummyScm(self.recipe), [{'file': 'change.patch'}], recipeSet=object())

    def test_modified_overlay_refuses_inline_reverse(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        old = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(old.invoke(invoker, False))
        overlay = {'fingerprint': hashDirectory(os.path.join(self.workspace, 'src')).hex()}
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('user edit\n')

        with self.assertRaises(ParseError):
            asyncio.run(old.switch(invoker, self.scm(old.getProperties(False)['patches'], overlay)))

    def test_rejects_scm_metadata_and_protected_url_download(self):
        invoker = Invoker(self.workspace)
        for target in ('.git/config', 'archive.tar'):
            self.writePatch('--- /dev/null\n+++ b/{}\n@@ -0,0 +1 @@\n+x\n'.format(target))
            scm = PatchScm(DummyScm(self.recipe, protected=('archive.tar',)),
                           [{'file': 'change.patch'}])
            with self.assertRaises(ParseError):
                asyncio.run(scm.invoke(invoker, False))

        self.writePatch('--- a/.git/config\n+++ /dev/null\n@@ -1 +0,0 @@\n-x\n')
        scm = self.scm([{'file': 'change.patch'}])
        with self.assertRaises(ParseError):
            asyncio.run(scm.invoke(invoker, False))

    def test_rejects_the_actual_url_download_path(self):
        self.writePatch('--- /dev/null\n+++ b/archive.tar\n@@ -0,0 +1 @@\n+x\n')
        url = UrlScm({'scm': 'url', 'url': 'file:///tmp/archive.tar',
                      'recipe': self.recipe, '__source': 'test', 'dir': 'src'})
        with self.assertRaises(ParseError):
            asyncio.run(PatchScm(url, [{'file': 'change.patch'}], patchOnly=True).invoke(
                Invoker(self.workspace), False))

    def test_matching_overlay_fingerprint_is_not_modified(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        status = scm.status(self.workspace, {
            'fingerprint': hashDirectory(os.path.join(self.workspace, 'src')).hex()})
        self.assertEqual(str(status), 'P')
    def test_applies_multiple_hunks_and_preserves_missing_final_newline(self):
        with open(os.path.join(self.workspace, 'src', 'message'), 'wb') as f:
            f.write(b'one\ntwo\nthree')
        self.writePatch('--- a/message\n+++ b/message\n'
                        '@@ -1 +1 @@\n-one\n+ONE\n'
                        '@@ -3 +3 @@\n-three\n\\ No newline at end of file\n'
                        '+THREE\n\\ No newline at end of file\n', newline='\n')
        asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'message'), 'rb') as f:
            self.assertEqual(f.read(), b'ONE\ntwo\nTHREE')

    def test_applies_and_reverses_crlf_content(self):
        target = os.path.join(self.workspace, 'src', 'message')
        with open(target, 'wb') as f:
            f.write(b'old\r\n')
        with open(os.path.join(self.temp.name, 'change.patch'), 'wb') as f:
            f.write(b'--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\r\n+new\r\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'new\r\n')
        asyncio.run(scm._PatchScm__unapply(invoker))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'old\r\n')

    def test_applies_and_reverses_crlf_patch_headers(self):
        target = os.path.join(self.workspace, 'src', 'message')
        with open(target, 'wb') as f:
            f.write(b'old\r\n')
        with open(os.path.join(self.temp.name, 'change.patch'), 'wb') as f:
            f.write(b'--- a/message\r\n+++ b/message\r\n@@ -1 +1 @@\r\n-old\r\n+new\r\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'new\r\n')
        asyncio.run(scm._PatchScm__unapply(invoker))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'old\r\n')

    def test_crlf_patch_preserves_missing_final_newline(self):
        target = os.path.join(self.workspace, 'src', 'message')
        with open(target, 'wb') as f:
            f.write(b'old')
        with open(os.path.join(self.temp.name, 'change.patch'), 'wb') as f:
            f.write(b'--- a/message\r\n+++ b/message\r\n@@ -1 +1 @@\r\n'
                    b'-old\r\n\\ No newline at end of file\r\n'
                    b'+new\r\n\\ No newline at end of file\r\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'new')
        asyncio.run(scm._PatchScm__unapply(invoker))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'old')

    def test_no_newline_preserves_a_literal_trailing_carriage_return(self):
        target = os.path.join(self.workspace, 'src', 'message')
        with open(target, 'wb') as f:
            f.write(b'old\r')
        with open(os.path.join(self.temp.name, 'change.patch'), 'wb') as f:
            f.write(b'--- a/message\n+++ b/message\n@@ -1 +1 @@\n'
                    b'-old\r\n\\ No newline at end of file\n'
                    b'+new\r\n\\ No newline at end of file\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'new\r')
        asyncio.run(scm._PatchScm__unapply(invoker))
        with open(target, 'rb') as f:
            self.assertEqual(f.read(), b'old\r')

    def test_finds_a_unique_nearby_hunk_location_forward_and_reverse(self):
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('intro\nold\n')
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        asyncio.run(scm._PatchScm__unapply(invoker))

        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'intro\nold\n')
        self.assertEqual(len(invoker.infoMessages), 2)
        self.assertTrue(all('offset 1 line' in message
                            for message in invoker.infoMessages))

    def test_nearby_hunks_keep_prior_hunk_line_delta(self):
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('old\nfiller\nfiller2\nanchor\n')
        self.writePatch('--- a/message\n+++ b/message\n'
                        '@@ -1 +1,2 @@\n-old\n+OLD\n+inserted\n'
                        '@@ -3 +4 @@\n-anchor\n+ANCHOR\n')
        invoker = Invoker(self.workspace)
        asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(invoker, False))

        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertEqual(f.read(), 'OLD\ninserted\nfiller\nfiller2\nANCHOR\n')
        self.assertEqual(len(invoker.infoMessages), 1)
        self.assertIn('offset 1 line', invoker.infoMessages[0])

    def test_gnu_patch_selects_a_nearby_hunk_location(self):
        with open(os.path.join(self.workspace, 'src', 'message'), 'w') as f:
            f.write('old\nmiddle\nold\n')
        self.writePatch('--- a/message\n+++ b/message\n@@ -2 +2 @@\n-old\n+new\n')
        asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
            Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'message')) as f:
            self.assertIn('new\n', f.read())

    def test_keeps_zero_source_line_insertions_at_declared_location(self):
        self.writePatch('--- a/message\n+++ b/message\n'
                        '@@ -1 +0,0 @@\n-old\n'
                        '@@ -0,0 +1 @@\n+new\n')
        with self.assertRaises(ParseError):
            asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(
                Invoker(self.workspace), False))

    def test_adds_and_removes_file_with_reverse(self):
        self.writePatch('--- /dev/null\n+++ b/added\n@@ -0,0 +1,2 @@\n+one\n+two\n')
        scm = self.scm([{'file': 'change.patch'}])
        invoker = Invoker(self.workspace)
        asyncio.run(scm.invoke(invoker, False))
        with open(os.path.join(self.workspace, 'src', 'added')) as f:
            self.assertEqual(f.read(), 'one\ntwo\n')
        asyncio.run(scm._PatchScm__unapply(invoker))
        self.assertFalse(os.path.exists(os.path.join(self.workspace, 'src', 'added')))

    def test_deletes_file_and_honors_strip_level(self):
        with open(os.path.join(self.workspace, 'src', 'remove-me'), 'w') as f:
            f.write('gone\n')
        self.writePatch('--- remove-me\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n')
        asyncio.run(self.scm([{'file': 'change.patch', 'strip': 0}]).invoke(
            Invoker(self.workspace), False))
        self.assertFalse(os.path.exists(os.path.join(self.workspace, 'src', 'remove-me')))

    def test_accepts_descriptive_preamble_and_orig_backup_header(self):
        with open(os.path.join(self.workspace, 'src', 'setupinfo.py'), 'w') as f:
            f.write('old\n')
        self.writePatch('This patch explains why the change is needed.\n\n'
                        '--- workspace/setupinfo.py.orig\t2025-03-12 22:36:10 +0100\n'
                        '+++ workspace/setupinfo.py\t2025-03-12 22:36:13 +0100\n'
                        '@@ -1 +1 @@\n-old\n+new\n')
        asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(Invoker(self.workspace), False))
        with open(os.path.join(self.workspace, 'src', 'setupinfo.py')) as f:
            self.assertEqual(f.read(), 'new\n')

    def test_rejects_shifted_malformed_and_unsupported_patches(self):
        invoker = Invoker(self.workspace)
        for body in (
                '--- a/message\n+++ b/message\n@@ -1,2 +1 @@\n-old\n+new\n',
                'diff --git a/message b/message\n--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+new\n',
                'GIT binary patch\nliteral 1\n'):
            self.writePatch(body)
            with self.assertRaises(ParseError):
                asyncio.run(self.scm([{'file': 'change.patch'}]).invoke(invoker, False))
    def test_jenkins_uses_serialized_pre_run_overlay(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        scm = self.scm([{'file': 'change.patch'}])
        self.assertFalse(scm.hasJenkinsPlugin())
        self.assertIn('__data', scm.getProperties(True)['patches'][0])

    def test_patched_jenkins_scm_runs_complete_checkout_in_bob(self):
        self.writePatch('--- a/message\n+++ b/message\n@@ -1 +1 @@\n-old\n+first\n')
        base = PluginScm(self.recipe)
        scm = PatchScm(base, [{'file': 'change.patch'}])
        self.assertFalse(scm.hasJenkinsPlugin())
        asyncio.run(scm.invoke(Invoker(self.workspace), False))
        self.assertTrue(base.invoked)
