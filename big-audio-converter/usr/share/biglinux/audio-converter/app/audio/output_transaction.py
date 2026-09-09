"""Stage outputs privately and publish without replacing an existing inode."""

import ctypes
import errno
import hashlib
import os
from pathlib import Path
import shutil
import tempfile


def available_path(path):
    """Suggest a free name. Publication still checks collisions atomically."""
    candidate = Path(path)
    stem, suffix = candidate.stem, candidate.suffix
    encoded = os.fsencode(stem)
    if len(encoded) + len(os.fsencode(suffix)) > 220:
        digest = hashlib.sha256(encoded).hexdigest()[:12]
        stem = encoded[:200 - len(os.fsencode(suffix))].decode("utf-8", "ignore") + "-" + digest
    candidate = candidate.with_name(stem + suffix)
    index = 1
    while os.path.lexists(candidate):
        candidate = candidate.with_name(f"{stem}-{index}{suffix}")
        index += 1
    return str(candidate)


def _publish_no_replace(source, target):
    """Use Linux RENAME_NOREPLACE or atomic hard-link creation.

    There is no exists()+replace() or copy() fallback: neither implements
    no-clobber semantics. Unsupported filesystems fail without replacing data.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    rename = getattr(libc, "renameat2", None)
    if rename is not None:
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), target)
    os.link(source, target, follow_symlinks=False)
    os.unlink(source)


class OutputTransaction:
    """Validate all media before publication and roll back owned outputs.

    Multiple filenames cannot be atomically visible as a group. Rollback is
    best-effort and only removes inodes created by this transaction, never
    preexisting files. Temporary files are on the destination filesystem.
    """

    def __init__(self, destinations, rename_on_conflict=True):
        self.destinations = [str(Path(p).absolute()) for p in destinations]
        self.rename_on_conflict = rename_on_conflict
        self.staged = []
        self._directories = []
        self._published = []
        self._committed = False

    def __enter__(self):
        try:
            for destination in self.destinations:
                parent = Path(destination).parent
                parent.mkdir(parents=True, exist_ok=True)
                directory = tempfile.mkdtemp(prefix=".bac-", dir=parent)
                self._directories.append(directory)
                staged = os.path.join(directory, "output" + Path(destination).suffix)
                self.staged.append(staged)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def commit(self):
        for staged, requested in zip(self.staged, self.destinations, strict=True):
            os.chmod(staged, 0o600)
            with open(staged, "rb") as stream:
                os.fsync(stream.fileno())
                identity = os.fstat(stream.fileno())
            target = available_path(requested) if self.rename_on_conflict else requested
            while True:
                try:
                    _publish_no_replace(staged, target)
                    break
                except FileExistsError:
                    if not self.rename_on_conflict:
                        raise
                    target = available_path(requested)
            self._published.append((target, identity.st_dev, identity.st_ino))
        for parent in {str(Path(path).parent) for path, _, _ in self._published}:
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                try:
                    os.fsync(fd)
                except OSError as error:
                    if error.errno not in (errno.EINVAL, errno.EOPNOTSUPP):
                        raise
            finally:
                os.close(fd)
        self._committed = True
        return tuple(path for path, _, _ in self._published)

    def __exit__(self, exc_type, exc, traceback):
        if not self._committed:
            for path, device, inode in reversed(self._published):
                try:
                    current = os.stat(path, follow_symlinks=False)
                    if (current.st_dev, current.st_ino) == (device, inode):
                        os.unlink(path)
                except FileNotFoundError:
                    pass
        for directory in self._directories:
            shutil.rmtree(directory, ignore_errors=True)
        return False
