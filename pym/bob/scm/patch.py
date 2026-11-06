# Bob build tool
# SPDX-License-Identifier: GPL-3.0-or-later

"""A transport-independent, recipe-local unified-diff overlay."""

from ..errors import ParseError
from .scm import Scm, ScmStatus, ScmTaint, overlayFingerprint
import base64
import asyncio
import glob
import hashlib
import os
import re
import schema
import stat


# Changing this means the checkout identity changes too.  Keep it separate
# from Bob's general version: it describes precisely the patch language Bob
# promises to interpret.
_PATCH_FORMAT_VERSION = 'gnu-patch-v1'
_HUNK_HEADER = re.compile(br'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?\r?\n?$')


class _PatchFormatError(Exception):
    pass


class PatchApplyError(ParseError):
    """Application failure, optionally with a verified retained base tree."""

    def __init__(self, message, directory, baseFingerprint=None):
        super().__init__(message)
        self.directory = directory
        self.baseFingerprint = baseFingerprint


class _PatchTargets:
    """Validate targets and track whether an invocation may have changed them."""

    def __init__(self, root, protected=(), beforeWrite=None):
        self.root = os.path.realpath(root)
        self.protected = {os.path.normcase(os.path.realpath(os.path.join(self.root, p)))
                          for p in protected}
        self.exists = {}
        self.untouched = True
        self.messages = []
        self.beforeWrite = beforeWrite

    def path(self, target):
        if target is None:
            return None
        path = os.path.join(self.root, target)
        resolved = os.path.realpath(path)
        if os.path.commonpath((self.root, resolved)) != self.root:
            raise _PatchFormatError("target '{}' escapes checkout".format(target))
        relative = os.path.relpath(resolved, self.root)
        if (os.path.normcase(resolved) in self.protected or
                any(p in ('.git', '.svn', 'CVS') for p in relative.split(os.sep))):
            raise _PatchFormatError("target '{}' modifies protected SCM path".format(target))
        # Reject symlinks rather than replacing their referents or leaving
        # dangling links behind when a patch deletes a file.
        if os.path.normcase(os.path.abspath(path)) != os.path.normcase(resolved):
            raise _PatchFormatError("symlink target '{}' is not supported".format(target))
        if path not in self.exists:
            try:
                mode = os.lstat(path).st_mode
                if not stat.S_ISREG(mode):
                    raise _PatchFormatError("target '{}' is not a regular file".format(target))
                self.exists[path] = True
            except FileNotFoundError:
                self.exists[path] = False
            except OSError as e:
                raise _PatchFormatError("cannot inspect '{}': {}".format(target, e))
        return path

def _header_path(line):
    """Return a unified-diff header path, excluding its optional timestamp."""
    path = line[4:].split(b'\t', 1)[0].rstrip(b'\r\n')
    if not path:
        raise _PatchFormatError("empty file name")
    try:
        return path.decode('utf-8')
    except UnicodeDecodeError:
        raise _PatchFormatError("file names must be UTF-8")


def _target_path(name, strip):
    if name == '/dev/null':
        return None
    parts = name.replace('\\', '/').split('/')
    target = '/'.join(parts[strip:])
    if not target or target.startswith('/') or '..' in parts[strip:]:
        raise _PatchFormatError("invalid target '{}'".format(name))
    target = os.path.normpath(target)
    if target == '.' or os.path.isabs(target) or os.path.splitdrive(target)[0]:
        raise _PatchFormatError("invalid target '{}'".format(name))
    return target


def _parse_patch(data, strip):
    """Parse the deliberately small, deterministic unified-diff dialect."""
    if b'\0' in data:
        raise _PatchFormatError("binary patches are not supported")
    lines = data.splitlines(keepends=True)
    files = []
    i = 0
    # A human-readable mail-style description before the first file is common
    # for recipe patches.  It has no effect on application, but is retained in
    # the patch digest.  Do not mistake known non-unified patch formats for a
    # description.
    while i < len(lines) and not lines[i].startswith(b'--- '):
        if lines[i].startswith((b'GIT binary patch', b'Binary files ', b'diff ',
                                b'Index: ', b'*** ')):
            raise _PatchFormatError("unsupported patch format at line {}".format(i + 1))
        i += 1
    while i < len(lines):
        if not lines[i].startswith(b'--- '):
            if lines[i].startswith((b'GIT binary patch', b'Binary files ')):
                raise _PatchFormatError("binary patches are not supported")
            raise _PatchFormatError("expected '---' file header at line {}".format(i + 1))
        old_name = _header_path(lines[i])
        i += 1
        if i == len(lines) or not lines[i].startswith(b'+++ '):
            raise _PatchFormatError("expected '+++' file header at line {}".format(i + 1))
        new_name = _header_path(lines[i])
        i += 1
        old_target = _target_path(old_name, strip)
        new_target = _target_path(new_name, strip)
        if old_target is None and new_target is None:
            raise _PatchFormatError("both file names are /dev/null")
        if old_target is not None and new_target is not None and old_target != new_target:
            # diff -u foo.orig foo is a conventional way to distribute a
            # modification.  It is not a rename: apply against foo itself.
            if old_target == new_target + '.orig':
                old_target = new_target
            else:
                raise _PatchFormatError("renames are not supported")
        hunks = []
        while i < len(lines) and lines[i].startswith(b'@@ '):
            match = _HUNK_HEADER.match(lines[i])
            if match is None:
                raise _PatchFormatError("malformed hunk header at line {}".format(i + 1))
            old_start, old_count, new_start, new_count = match.groups()
            old_start, new_start = int(old_start), int(new_start)
            old_count = int(old_count) if old_count is not None else 1
            new_count = int(new_count) if new_count is not None else 1
            if (old_count and not old_start) or (new_count and not new_start):
                raise _PatchFormatError("nonempty hunk has invalid location at line {}"
                                        .format(i + 1))
            i += 1
            records = []
            actual_old = actual_new = 0
            while i < len(lines):
                line = lines[i]
                if line in (b'\\ No newline at end of file\n',
                            b'\\ No newline at end of file\r\n',
                            b'\\ No newline at end of file'):
                    if not records or not records[-1][1].endswith(b'\n'):
                        raise _PatchFormatError("misplaced no-newline marker at line {}".format(i + 1))
                    records[-1] = (records[-1][0], records[-1][1][:-1])
                    i += 1
                    continue
                if actual_old == old_count and actual_new == new_count:
                    break
                if not line or line[:1] not in (b' ', b'+', b'-'):
                    raise _PatchFormatError("invalid hunk line at line {}".format(i + 1))
                records.append((line[:1], line[1:]))
                actual_old += line[:1] in (b' ', b'-')
                actual_new += line[:1] in (b' ', b'+')
                if actual_old > old_count or actual_new > new_count:
                    raise _PatchFormatError("hunk line count does not match header")
                i += 1
            if actual_old != old_count or actual_new != new_count:
                raise _PatchFormatError("hunk line count does not match header")
            hunks.append((old_start, old_count, new_start, new_count, records))
        if not hunks:
            raise _PatchFormatError("file '{}' has no hunks".format(old_name))
        files.append((old_target, new_target, hunks))
    if not files:
        raise _PatchFormatError("empty patch")
    return files


class _Patch:
    """One resolved patch and the metadata needed to replay it safely."""

    def __init__(self, path, name, strip, digest, data):
        self.path = path
        self.name = name
        self.strip = strip
        self.digest = digest
        self.data = data
        self.__parsed = None

    @classmethod
    def fromSpec(cls, root, name, spec, loadBinary=None):
        data = spec.get('__data')
        if data is not None:
            try:
                data = base64.b64decode(data, validate=True)
            except (TypeError, ValueError) as e:
                raise ParseError("Invalid persisted patch '{}': {}".format(name, e))
            digest = hashlib.sha256(data).hexdigest()
            if digest != spec.get('digestSHA256', digest):
                raise ParseError("Persisted patch digest does not match '{}'".format(name))
            path = None
        else:
            path = os.path.join(root, name)
            try:
                if loadBinary is None:
                    with open(path, 'rb') as f:
                        data = f.read()
                else:
                    # Besides reading the data this registers the patch with
                    # RecipeSet's package-cache digest.
                    data = loadBinary(path)
            except OSError as e:
                raise ParseError("Cannot read patch '{}': {}".format(name, e))
            digest = hashlib.sha256(data).hexdigest()

        strip = spec.get('strip', 1)
        if not isinstance(strip, int) or isinstance(strip, bool) or strip < 0:
            raise ParseError("Invalid patch strip level: '{}'".format(strip))
        return cls(path, name, strip, digest, data)

    def asProperty(self):
        return {'file': self.name, 'strip': self.strip,
                'digestSHA256': self.digest,
                '__data': base64.b64encode(self.data).decode('ascii')}

    def asManifestEntry(self):
        return {'path': self.name, 'strip': self.strip,
                'digestSHA256': self.digest}

    def withStrip(self, strip):
        """Replay these bytes using an updated target-path configuration."""
        return _Patch(self.path, self.name, strip, self.digest, self.data)

    def validateTargets(self, protected):
        try:
            parsed = self.__parse()
        except _PatchFormatError as e:
            raise ParseError("Unsupported patch '{}': {}".format(self.name, e))
        protected = {os.path.normcase(os.path.normpath(p)) for p in protected}
        for old_target, new_target, hunks in parsed:
            for target in (old_target, new_target):
                if target is None:
                    continue
                if (any(p in ('.git', '.svn', 'CVS') for p in target.replace('\\', '/').split('/')) or
                        os.path.normcase(target) in protected):
                    raise ParseError("Patch modifies protected SCM path: '{}'".format(target))

    def __parse(self):
        if self.__parsed is None:
            self.__parsed = _parse_patch(self.data, self.strip)
        return self.__parsed

    async def run(self, invoker, directory, reverse=False, transaction=None):
        """Apply stored bytes with GNU Patch after a non-mutating dry run."""
        try:
            ownTransaction = transaction is None
            if ownTransaction:
                transaction = _PatchTargets(invoker.joinPath(directory))
            parsed = self.__parse()
            try:
                version = await asyncio.create_subprocess_exec(
                    'patch', '--version', stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT)
                versionOutput, _ = await version.communicate()
            except OSError as e:
                raise _PatchFormatError("cannot run GNU Patch: {}".format(e))
            if version.returncode or b'GNU patch' not in versionOutput:
                raise _PatchFormatError("GNU Patch is required; 'patch --version' "
                                        "did not identify GNU Patch")
            targets = {}
            for old_target, new_target, hunks in parsed:
                for target in (old_target, new_target):
                    if target is not None:
                        targets[target] = transaction.path(target)
                source, destination = ((new_target, old_target) if reverse
                                       else (old_target, new_target))
                if source is None and transaction.exists[targets[destination]]:
                    raise _PatchFormatError("creation target '{}' already exists".format(destination))
            args = ['patch', '--batch', '--fuzz=0', '--binary',
                    '--no-backup-if-mismatch', '--reject-file=-',
                    '-p{}'.format(self.strip)]
            args.append('--reverse' if reverse else '--forward')
            patchData = self.data
            # First try the original bytes: a trailing CR may itself be part
            # of a file without a newline. If that fails, a CRLF-transported
            # patch may instead need its terminator normalized at the marker.
            normalized = self.data.replace(b'\r\n\\ No newline at end of file',
                                           b'\n\\ No newline at end of file')
            for dryRun in (True, False):
                candidates = (patchData, normalized) if dryRun and normalized != patchData else (patchData,)
                for candidate in candidates:
                    try:
                        if not dryRun:
                            # A real invocation can fail after changing a file.
                            if transaction.beforeWrite is not None:
                                transaction.beforeWrite()
                            transaction.untouched = False
                        process = await asyncio.create_subprocess_exec(
                            *(args + (['--dry-run'] if dryRun else [])), cwd=transaction.root,
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.STDOUT)
                        output, _ = await process.communicate(candidate)
                    except OSError as e:
                        raise _PatchFormatError("cannot run GNU Patch: {}".format(e))
                    if not process.returncode:
                        patchData = candidate
                        break
                else:
                    raise _PatchFormatError(output.decode(errors='replace').strip())
                if not dryRun and output.strip():
                    transaction.messages.append(output.decode(errors='replace').strip())
            # Later patches in the stack must see files changed by this one.
            for path in targets.values():
                transaction.exists[path] = os.path.isfile(path)
            if ownTransaction:
                for message in transaction.messages:
                    invoker.info(message)
        except _PatchFormatError as e:
            raise ParseError("Cannot apply patch '{}': {}".format(self.name, e))


class PatchScm(Scm):
    PATCHES_SHEMA = [schema.Schema({
        schema.Or('file', 'files', 'series') : str,
        schema.Optional('strip') : int,
    })]

    def __init__(self, scm, patches, overlay=None, patchOnly=False, overlayFailed=False,
                 recipeSet=None, failedBase=None):
        self.__scm = scm
        self.__patches = self.__resolve(patches, recipeSet)
        self.__overlay = overlay
        self.__patchOnly = patchOnly
        self.__overlayFailed = overlayFailed
        self.__failedBase = failedBase

    def __resolve(self, patches, recipeSet):
        root = os.path.dirname(self.__scm._getRecipe().split('#', 1)[0])
        names = self.__resolvePaths(root, patches)
        loadBinary = getattr(recipeSet, 'loadBinary', None)
        return [_Patch.fromSpec(root, name, patch,
                                loadBinary)
                for name, patch in names]

    @classmethod
    def trackPatchFiles(cls, recipe, patches, loadBinary):
        """Include recipe patch files in the persistent package-cache key."""
        root = os.path.dirname(recipe.split('#', 1)[0])
        for patch in patches:
            if 'series' in patch:
                cls.__validateRecipePath(patch['series'])
                cls.__validateRecipeFile(root, patch['series'])
                loadBinary(os.path.join(root, patch['series']))
        for name, patch in cls.__resolvePaths(root, patches):
            loadBinary(os.path.join(root, name))

    @classmethod
    def __resolvePaths(cls, root, patches):
        ret = []
        for patch in patches:
            if 'file' in patch:
                files = [patch['file']]
            elif 'files' in patch:
                pattern = patch['files']
                cls.__validateRecipePath(pattern)
                if '**' in pattern:
                    raise ParseError("Recursive patch glob is not supported: '{}'".format(pattern))
                files = [os.path.relpath(path, root) for path in
                         sorted(glob.glob(os.path.join(root, pattern)))
                         if os.path.isfile(path)]
                if not files:
                    raise ParseError("Patch glob '{}' did not match any files".format(pattern))
            elif 'series' in patch:
                cls.__validateRecipePath(patch['series'])
                cls.__validateRecipeFile(root, patch['series'])
                series = os.path.join(root, patch['series'])
                try:
                    with open(series, encoding='utf-8') as f:
                        files = []
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith('#'):
                                continue
                            fields = line.split()
                            if len(fields) != 1:
                                raise ParseError("Patch series '{}' has unsupported options"
                                                 .format(patch['series']))
                            files.append(fields[0])
                except OSError as e:
                    raise ParseError("Cannot read patch series '{}': {}".format(patch['series'], e))
                files = [os.path.join(os.path.dirname(patch['series']), f) for f in files]
            else:
                raise ParseError("Patch entry needs 'file', 'files', or 'series'")
            for name in files:
                cls.__validateRecipePath(name)
                if '__data' not in patch:
                    cls.__validateRecipeFile(root, name)
                ret.append((name, patch))
        return ret

    @staticmethod
    def __validateRecipePath(name):
        if os.path.isabs(name) or '..' in os.path.normpath(name).split(os.sep):
            raise ParseError("Patch path escapes recipe directory: '{}'".format(name))

    @staticmethod
    def __validateRecipeFile(root, name):
        root = os.path.realpath(root)
        path = os.path.realpath(os.path.join(root, name))
        try:
            inside = os.path.normcase(os.path.commonpath((root, path))) == os.path.normcase(root)
        except ValueError:
            inside = False
        if not inside:
            raise ParseError("Patch path escapes recipe directory: '{}'".format(name))

    def getProperties(self, isJenkins, pretty=False):
        ret = self.__scm.getProperties(isJenkins, pretty)
        ret['patches'] = [patch.asProperty() for patch in self.__patches]
        return ret

    async def invoke(self, invoker, workspaceCreated):
        if self.__overlay is not None and not self.__patchOnly:
            fingerprint = overlayFingerprint(invoker.joinPath(self.getDirectory()))
            if fingerprint != self.__overlay.get('fingerprint'):
                raise ParseError("Patched checkout was modified; refusing to reuse managed overlay")
            await self.__scm.invokeWithCheckoutHooks(
                invoker, workspaceCreated, lambda: self.__unapply(invoker),
                lambda: self.__apply(invoker))
            return
        if not self.__patchOnly:
            await self.__scm.invoke(invoker, workspaceCreated)
        await self.__apply(invoker)

    async def __apply(self, invoker):
        protected = set(self.__scm.getProtectedPaths())
        directory = self.__scm.getDirectory()
        transaction = _PatchTargets(invoker.joinPath(directory), protected,
                                    self.checkpointCheckoutProgress)
        base = overlayFingerprint(transaction.root)
        try:
            for patch in self.__patches:
                patch.validateTargets(protected)
                await patch.run(invoker, directory, transaction=transaction)
            for message in transaction.messages:
                invoker.info(message)
        except (ParseError, _PatchFormatError) as e:
            raise PatchApplyError(e.slogan if isinstance(e, ParseError) else str(e), directory,
                                  base if transaction.untouched else None) from e

    async def __unapply(self, invoker, stripOverrides=None):
        if self.__overlay is not None:
            path = invoker.joinPath(self.__scm.getDirectory())
            fingerprint = overlayFingerprint(path)
            if fingerprint != self.__overlay.get('fingerprint'):
                raise ParseError("Patched checkout was modified; refusing to reverse managed overlay")
        protected = set(self.__scm.getProtectedPaths())
        transaction = _PatchTargets(invoker.joinPath(self.__scm.getDirectory()), protected,
                                    self.checkpointCheckoutProgress)
        for patch in reversed(self.__patches):
            if stripOverrides is not None and patch.name in stripOverrides:
                patch = patch.withStrip(stripOverrides[patch.name])
            patch.validateTargets(protected)
            await patch.run(invoker, self.__scm.getDirectory(), reverse=True,
                            transaction=transaction)
        for message in transaction.messages:
            invoker.info(message)

    def canSwitch(self, oldScm):
        """A patch-only recipe change is an inline SCM transition.

        The old patch contents and expected workspace fingerprint are persisted
        with the checkout state, so a changed stack can be reversed safely.
        """
        return (isinstance(oldScm, PatchScm) and
                (not oldScm.__overlayFailed or oldScm.__failedBase is not None) and
                self.__scm.canSwitch(oldScm.__scm))

    async def switch(self, invoker, oldScm):
        if oldScm.__overlayFailed:
            if (oldScm.__failedBase is None or
                    overlayFingerprint(invoker.joinPath(self.getDirectory())) != oldScm.__failedBase):
                raise ParseError("Failed overlay has no verified unchanged base; fresh checkout required")
            return await self.__scm.switch(invoker, oldScm.__scm)
        if oldScm.__overlay is None:
            # Legacy state without a managed-overlay fingerprint cannot
            # establish that the old patch was applied.
            # Leave the base checkout in place for the corrected overlay.
            return await self.__scm.switch(invoker, oldScm.__scm)
        # Keep old bytes for a safe reverse, but use the current path mapping.
        # A corrected strip level must be able to recover an existing checkout.
        strips = {patch.name: patch.strip for patch in self.__patches}
        await oldScm.__unapply(invoker, strips)
        # The normal checkout invocation following a successful inline switch
        # refreshes the base SCM and applies this overlay exactly once.
        return await self.__scm.switch(invoker, oldScm.__scm)

    def asDigestScript(self):
        return self.__scm.asDigestScript() + ' patch-format {}'.format(_PATCH_FORMAT_VERSION) + ''.join(
            ' patch {} p{}'.format(patch.digest, patch.strip)
            for patch in self.__patches)

    def getOverlayState(self):
        """Return serializable manifest data for Bob's checkout state."""
        return [patch.asManifestEntry() for patch in self.__patches]

    def setCheckoutState(self, state):
        self.__overlay = state

    def setCheckoutFailureState(self, state):
        self.__overlayFailed = state is not None
        # Version 9 development checkouts stored the fingerprint directly.
        self.__failedBase = (state if isinstance(state, str) else
                             state.get('baseFingerprint') if state is not None else None)

    def canRetryCheckout(self, workspacePath):
        return (self.__failedBase is not None and os.path.exists(workspacePath) and
                overlayFingerprint(workspacePath) == self.__failedBase)

    def getCheckoutState(self, workspacePath):
        return {
            'manifest': self.getOverlayState(),
            'fingerprint': overlayFingerprint(os.path.join(workspacePath, self.getDirectory())),
        }

    def hasCheckoutState(self):
        return True

    def getCheckoutFailureState(self, error):
        if (isinstance(error, PatchApplyError) and error.directory == self.getDirectory()
                and error.baseFingerprint is not None):
            return {'baseFingerprint': error.baseFingerprint}
        return None

    def getDirectory(self): return self.__scm.getDirectory()
    def getSource(self): return self.__scm.getSource()
    def hasJenkinsPlugin(self):
        # Plugin checkout runs before Bob can remove an existing overlay.
        return False
    def isDeterministic(self): return self.__scm.isDeterministic()
    def isLocal(self): return self.__scm.isLocal()
    def hasLiveBuildId(self): return self.__scm.hasLiveBuildId()
    async def predictLiveBuildId(self, step): return await self.__scm.predictLiveBuildId(step)
    def calcLiveBuildId(self, workspacePath): return self.__scm.calcLiveBuildId(workspacePath)
    def getAuditSpec(self):
        spec = self.__scm.getAuditSpec()
        if spec is None:
            return None
        typ, directory, extra = spec
        extra = extra.copy()
        extra['patches'] = self.getOverlayState()
        extra['patchDirectory'] = self.getDirectory()
        return typ, directory, extra
    def status(self, workspacePath, overlay=None):
        return self.statusWithCheckoutState(workspacePath, overlay)

    def statusWithCheckoutState(self, workspacePath, overlay=None):
        status = self.__scm.status(workspacePath)
        if overlay is not None:
            path = os.path.join(workspacePath, self.__scm.getDirectory())
            fingerprint = overlayFingerprint(path)
            if fingerprint == overlay.get('fingerprint'):
                status.remove(ScmTaint.modified)
        status.add(ScmTaint.patched, 'Declared patch overlay')
        return status
    def getActiveOverrides(self): return self.__scm.getActiveOverrides()
    def postAttic(self, workspace): return self.__scm.postAttic(workspace)
