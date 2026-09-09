"""Publish validated outputs atomically without replacing existing files."""

import ctypes
import errno
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    _renameat2.restype = ctypes.c_int


def available_path(path):
    """Return a naming candidate; publication, not this check, prevents races."""
    path = Path(path)
    candidate = path
    index = 1
    while os.path.lexists(candidate):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        index += 1
    return str(candidate)


def _rename_no_replace(source, target):
    """Use Linux RENAME_NOREPLACE, with a no-clobber hard-link fallback."""
    if _renameat2 is not None:
        if _renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1) == 0:
            return
        code = ctypes.get_errno()
        if code not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise OSError(code, os.strerror(code), target)
    # Both names are in the same filesystem. Never fall back to copying into
    # an existing path or to os.replace(), which would overwrite user data.
    os.link(source, target, follow_symlinks=False)
    os.unlink(source)


@contextmanager
def staging_directory(output_path):
    parent = Path(output_path).absolute().parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".big-audio-converter-", dir=parent) as directory:
        yield Path(directory)


def publish_all(staged_outputs, runner):
    """Publish a fully validated set, rolling back our own files on failure.

    Atomicity is per file. A multi-file set cannot be atomically renamed as a
    unit; a power failure during publication may leave some complete files,
    but never an apparently complete file containing partial conversion data.
    """
    published = []
    try:
        for staged, requested in staged_outputs:
            runner.check_cancelled()
            with open(staged, "rb") as stream:
                os.fsync(stream.fileno())
            target = available_path(requested)
            while True:
                runner.check_cancelled()
                identity = os.stat(staged)
                try:
                    _rename_no_replace(str(staged), target)
                    break
                except FileExistsError:
                    target = available_path(requested)
            published.append((target, identity.st_dev, identity.st_ino))
        for parent in {str(Path(p[0]).parent) for p in published}:
            fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
            finally:
                os.close(fd)
        runner.check_cancelled()
        return tuple(p[0] for p in published)
    except BaseException:
        for path, device, inode in reversed(published):
            try:
                current = os.lstat(path)
                if (current.st_dev, current.st_ino) == (device, inode):
                    os.unlink(path)
            except FileNotFoundError:
                pass
        raise
