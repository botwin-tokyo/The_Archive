"""Linux-only descriptor-confined storage operations.

No realpath-prefix check is used as an authorization boundary. openat2 enforces
BENEATH | NO_SYMLINKS | NO_XDEV on every relative resolution; unsupported kernels
fail closed. Mutations pin parent descriptors, revalidate parents and the volume,
and use renameat2(RENAME_NOREPLACE). Directory flush failures are not hidden.

The managed root must be writable only by the service OS account. Directory fds
prevent symlink substitution from redirecting I/O, but POSIX cannot atomically
prove a directory remains beneath a root while a hostile privileged writer moves
that already-open directory elsewhere. Revalidation detects observed changes; it
is not a snapshot guarantee against arbitrary external writers or root.
"""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import ctypes
from .contracts import BackendError

from dataclasses import dataclass, asdict
import errno
import hashlib
import io
import os
import platform
import re
import stat as statmod
import threading
from typing import Iterator, Protocol


class _Volume(Protocol):
    generation: int
    def check(self, generation: int | None = None) -> None: ...
    def open_root(self) -> int: ...
    def capacity(self) -> dict: ...

RESOLVE_NO_XDEV = 0x01
RESOLVE_NO_SYMLINKS = 0x04
RESOLVE_BENEATH = 0x08
CONFINED_RESOLVE = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_XDEV
RENAME_NOREPLACE = 1
INTERNAL_ROOT = ".archive-console"
_RESERVED = frozenset((INTERNAL_ROOT, ".archive-staging", ".archive-trash", ".archive-state"))
_INTERNAL_AREAS = frozenset(("staging", "trash", "manifests"))
_LIBC = ctypes.CDLL(None, use_errno=True)
_LIBC.syscall.restype = ctypes.c_long


class FilesystemError(BackendError):
    def __init__(self, code: str, message: str, *, errno_value: int | None = None):
        self.errno = errno_value
        super().__init__(code, message)


class _OpenHow(ctypes.Structure):
    _fields_ = [("flags", ctypes.c_uint64), ("mode", ctypes.c_uint64), ("resolve", ctypes.c_uint64)]


def _raise_os(error: int, operation: str) -> None:
    codes = {errno.ENOENT: "NOT_FOUND", errno.EEXIST: "CONFLICT",
             errno.ELOOP: "INVALID_PATH", errno.EXDEV: "INVALID_PATH",
             errno.EROFS: "READ_ONLY", errno.EACCES: "PERMISSION_DENIED",
             errno.EPERM: "PERMISSION_DENIED", errno.ENOSPC: "INSUFFICIENT_SPACE",
             errno.ENOTDIR: "INVALID_PATH", errno.ENOTEMPTY: "CONFLICT"}
    raise FilesystemError(codes.get(error, "IO_ERROR"), operation + " failed", errno_value=error)


def openat2(dir_fd: int, path: str, flags: int, mode: int = 0, *, resolve: int = CONFINED_RESOLVE) -> int:
    """Invoke the real Linux syscall, with no unsafe compatibility fallback."""
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "aarch64", "arm64"):
        raise FilesystemError("UNSUPPORTED_ENTRY", "Linux x86-64 or ARM64 openat2 support is required")
    how = _OpenHow(flags, mode, resolve)
    result = _LIBC.syscall(ctypes.c_long(437), ctypes.c_int(dir_fd),
                           ctypes.c_char_p(os.fsencode(path)), ctypes.byref(how), ctypes.c_size_t(ctypes.sizeof(how)))
    if result < 0:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.E2BIG):
            raise FilesystemError("UNSUPPORTED_ENTRY", "Required openat2 confinement is unavailable", errno_value=error)
        _raise_os(error, "Confined path resolution")
    return int(result)


def _renameat2(source_fd: int, source: str, destination_fd: int, destination: str) -> None:
    machine = platform.machine()
    number = {"x86_64": 316, "aarch64": 276, "arm64": 276}.get(machine)
    if platform.system() != "Linux" or number is None:
        raise FilesystemError("UNSUPPORTED_ENTRY", "Linux renameat2 support is required")
    result = _LIBC.syscall(ctypes.c_long(number), ctypes.c_int(source_fd), ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_int(destination_fd), ctypes.c_char_p(os.fsencode(destination)), ctypes.c_uint(RENAME_NOREPLACE))
    if result < 0:
        error = ctypes.get_errno()
        if error in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
            raise FilesystemError("UNSUPPORTED_ENTRY", "Atomic no-replace rename is unavailable", errno_value=error)
        _raise_os(error, "Atomic no-replace rename")


def _reserved(name: str) -> bool:
    return name.casefold() in _RESERVED or name.casefold().startswith(".archive-")


def validate_name(name: str) -> str:
    if not isinstance(name, str) or not name or "/" in name or "\\" in name:
        raise FilesystemError("INVALID_PATH", "A single nonempty name without separators is required")
    validate_path(name, allow_root=False)
    return name


def validate_path(path: str, *, allow_root: bool = True, internal: bool = False) -> str:
    if not isinstance(path, str) or "\0" in path or "\\" in path:
        raise FilesystemError("INVALID_PATH", "Invalid logical relative path")
    if path == "" and allow_root:
        return path
    if not path or path.startswith("/") or path.endswith("/"):
        raise FilesystemError("INVALID_PATH", "Expected a relative path without a trailing separator")
    parts = path.split("/")
    if len(parts) > 64 or any(part in ("", ".", "..") for part in parts):
        raise FilesystemError("INVALID_PATH", "Traversal and excessive path depth are forbidden")
    try:
        if len(path.encode("utf-8", "strict")) > 4095 or any(len(p.encode("utf-8", "strict")) > 255 for p in parts):
            raise FilesystemError("INVALID_PATH", "Path or name is too long")
    except UnicodeError as exc:
        raise FilesystemError("INVALID_PATH", "Path is not valid Unicode") from exc
    if not internal and any(_reserved(part) for part in parts):
        raise FilesystemError("INVALID_PATH", "Backend internal paths are protected")
    return path


def _revision(info: os.stat_result) -> int:
    identity = (info.st_dev, info.st_ino, info.st_mode, info.st_nlink,
                info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return int.from_bytes(hashlib.sha256(repr(identity).encode("ascii")).digest()[:8], "big") & ((1 << 53) - 1)


def _kind(info: os.stat_result, *, allow_symlink: bool = True) -> str:
    if statmod.S_ISLNK(info.st_mode) and allow_symlink:
        return "symlink"
    if statmod.S_ISDIR(info.st_mode):
        return "directory"
    if statmod.S_ISREG(info.st_mode):
        if info.st_nlink != 1:
            raise FilesystemError("UNSUPPORTED_ENTRY", "Hard-linked files are not supported")
        return "file"
    raise FilesystemError("UNSUPPORTED_ENTRY", "Special files and symlink traversal are unsupported")


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    kind: str
    size: int
    mtime_ns: int
    revision: int
    device: int
    inode: int
    nlink: int
    mode: int

    @property
    def is_dir(self) -> bool:
        return self.kind == "directory"

    def to_dict(self) -> dict:
        return asdict(self)


def _entry(path: str, info: os.stat_result) -> Entry:
    return Entry(path.rsplit("/", 1)[-1], path, _kind(info), info.st_size,
                 info.st_mtime_ns, _revision(info), info.st_dev, info.st_ino,
                 info.st_nlink, statmod.S_IMODE(info.st_mode))


class CheckedFile:
    """A file stream checking mount generation and file identity at each I/O call.

    Read streams also check revision before and after reading. A changed source
    fails instead of returning that chunk. Callers must checkpoint before commit;
    no API claims snapshot semantics for externally writable files.
    """
    def __init__(self, file: io.BufferedIOBase, volume: _Volume, writable: bool):
        self._file, self._volume, self._writable = file, volume, writable
        self._generation = volume.generation
        self._initial = os.fstat(file.fileno())
        self.revision = _revision(self._initial)

    @property
    def closed(self):
        return self._file.closed

    def check(self) -> None:
        self._volume.check(self._generation)
        current = os.fstat(self._file.fileno())
        _kind(current, allow_symlink=False)
        if (current.st_dev, current.st_ino) != (self._initial.st_dev, self._initial.st_ino):
            raise FilesystemError("STALE_REVISION", "Open file identity changed")
        if not self._writable and _revision(current) != self.revision:
            raise FilesystemError("STALE_REVISION", "Source changed while reading")

    def read(self, size: int = -1) -> bytes:
        self.check()
        result = self._file.read(size)
        self.check()
        return result

    def readinto(self, buffer) -> int:
        result = self.read(len(buffer))
        buffer[:len(result)] = result
        return len(result)

    def write(self, data: bytes) -> int:
        if not self._writable:
            raise io.UnsupportedOperation("not writable")
        self.check()
        try:
            result = self._file.write(data)
        except OSError as exc:
            _raise_os(exc.errno, "Staged write")
        self.check()
        return result

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        self.check()
        return self._file.seek(offset, whence)

    def tell(self) -> int:
        self.check()
        return self._file.tell()

    def truncate(self, size: int | None = None) -> int:
        if not self._writable:
            raise io.UnsupportedOperation("not writable")
        self.check()
        result = self._file.truncate(size)
        self.check()
        return result

    def flush(self) -> None:
        """Durability checkpoint for staged bytes before an offset is acknowledged."""
        self.check()
        try:
            self._file.flush()
            if self._writable:
                os.fsync(self._file.fileno())
        except OSError as exc:
            _raise_os(exc.errno, "Staged data flush")
        self.check()

    def fileno(self) -> int:
        self.check()
        return self._file.fileno()

    def close(self) -> None:
        self._file.close()

    def __enter__(self):
        self.check()
        return self

    def __exit__(self, *_):
        self.close()


class ConfinedFilesystem:
    def __init__(self, volume: _Volume):
        self.volume = volume
        self._lock = threading.RLock()

    @contextmanager
    def _open(self, path: str, flags: int, mode: int = 0, *, internal: bool = False):
        validate_path(path, internal=internal)
        root = self.volume.open_root()
        fd = -1
        try:
            fd = openat2(root, path or ".", flags | os.O_CLOEXEC, mode)
            self.volume.check()
            yield fd
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(root)

    def _stat(self, path: str, *, internal: bool = False) -> Entry:
        with self._open(path, os.O_PATH | os.O_NOFOLLOW, internal=internal) as fd:
            result = _entry(path, os.fstat(fd))
            self.volume.check()
            return result

    def stat(self, path: str = "") -> Entry:
        return self._stat(path)

    def _assert_parent(self, path: str, fd: int, *, internal: bool = False) -> None:
        with self._open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=internal) as current:
            expected, actual = os.fstat(fd), os.fstat(current)
            if (expected.st_dev, expected.st_ino) != (actual.st_dev, actual.st_ino):
                raise FilesystemError("STALE_REVISION", "Parent directory changed")

    def _iter_entries(self, path: str, *, internal: bool = False) -> Iterator[Entry]:
        with self._open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=internal) as fd:
            with os.scandir(fd) as entries:
                for item in entries:
                    if not internal and _reserved(item.name):
                        continue
                    child = (path + "/" if path else "") + item.name
                    validate_path(child, internal=internal)
                    self._assert_parent(path, fd, internal=internal)
                    yield self._stat(child, internal=internal)
            self._assert_parent(path, fd, internal=internal)

    def iter_entries(self, path: str = "") -> Iterator[Entry]:
        """Streaming, unsorted entries; callers bound and snapshot their listing."""
        yield from self._iter_entries(path)

    def list(self, path: str = "", *, max_entries: int = 10000) -> list[Entry]:
        if not 0 <= max_entries <= 10000:
            raise FilesystemError("INVALID_PATH", "Invalid listing bound")
        result = []
        for entry in self.iter_entries(path):
            if len(result) >= max_entries:
                raise FilesystemError("TOO_MANY_ENTRIES", "Use bounded streaming catalog enumeration")
            result.append(entry)
        return result

    def _writable(self) -> None:
        if self.volume.capacity()["read_only"]:
            raise FilesystemError("READ_ONLY", "Volume is read-only")

    @staticmethod
    def _expect(entry: Entry, expected_revision: int | None) -> None:
        if expected_revision is not None and entry.revision != expected_revision:
            raise FilesystemError("STALE_REVISION", "Entry revision is stale")

    def _open_file(self, path: str, *, writable: bool = False, create: bool = False,
                   internal: bool = False, expected_revision: int | None = None) -> CheckedFile:
        validate_path(path, allow_root=False, internal=internal)
        if writable:
            self._writable()
        root = self.volume.open_root()
        fd = -1
        try:
            flags = (os.O_RDWR if writable else os.O_RDONLY) | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
            if create:
                # O_EXCL creates our own regular inode, never opens an existing
                # device, FIFO, socket or symlink.
                fd = openat2(root, path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            else:
                # Pin and inspect the inode before opening for I/O. Opening a
                # user pathname for reading and only then fstat() could trigger
                # device-driver side effects. /proc/self/fd is a kernel-owned,
                # descriptor-derived reference, never a caller-supplied path.
                pinned = openat2(root, path, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    identity = os.fstat(pinned)
                    if statmod.S_ISLNK(identity.st_mode):
                        raise FilesystemError("INVALID_PATH", "Symlink traversal is forbidden")
                    if _kind(identity, allow_symlink=False) != "file":
                        raise FilesystemError("UNSUPPORTED_ENTRY", "A regular independent file is required")
                    self._expect(_entry(path, identity), expected_revision)
                    try:
                        fd = os.open(f"/proc/self/fd/{pinned}", flags & ~os.O_NOFOLLOW)
                    except OSError as exc:
                        _raise_os(exc.errno, "Pinned file reopening")
                    opened = os.fstat(fd)
                    if (opened.st_dev, opened.st_ino) != (identity.st_dev, identity.st_ino):
                        raise FilesystemError("STALE_REVISION", "Pinned file identity changed")
                finally:
                    os.close(pinned)
            info = os.fstat(fd)
            if _kind(info, allow_symlink=False) != "file":
                raise FilesystemError("UNSUPPORTED_ENTRY", "A regular independent file is required")
            self._expect(_entry(path, info), expected_revision)
            self._expect(self._stat(path, internal=internal), _revision(info))
            self.volume.check()
            if create:
                parent = path.rpartition("/")[0]
                with self._open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=internal) as parent_fd:
                    self._assert_parent(parent, parent_fd, internal=internal)
                    os.fsync(parent_fd)
                published = self._stat(path, internal=internal)
                if (published.device, published.inode) != (info.st_dev, info.st_ino):
                    raise FilesystemError("STALE_REVISION", "Staging identity changed after creation")
            # Unbuffered I/O avoids flushing retained writes after invalidation.
            stream = os.fdopen(fd, "r+b" if writable else "rb", buffering=0)
            fd = -1
            return CheckedFile(stream, self.volume, writable)
        finally:
            if fd >= 0:
                os.close(fd)
            os.close(root)

    def open_read(self, path: str, expected_revision: int | None = None) -> CheckedFile:
        return self._open_file(path, expected_revision=expected_revision)

    def _create_dir(self, path: str, *, internal: bool = False) -> Entry:
        validate_path(path, allow_root=False, internal=internal)
        parent, _, name = path.rpartition("/")
        with self._lock:
            self._writable()
            with self._open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=internal) as fd:
                self._assert_parent(parent, fd, internal=internal)
                try:
                    os.mkdir(name, mode=0o700, dir_fd=fd)
                    os.fsync(fd)
                except OSError as exc:
                    _raise_os(exc.errno, "Directory creation")
                self._assert_parent(parent, fd, internal=internal)
            return self._stat(path, internal=internal)

    def create_dir(self, path: str) -> Entry:
        return self._create_dir(path)

    def _rename(self, source: str, destination: str, expected_revision: int | None = None,
                *, source_internal: bool = False, destination_internal: bool = False) -> Entry:
        validate_path(source, allow_root=False, internal=source_internal)
        validate_path(destination, allow_root=False, internal=destination_internal)
        with self._lock:
            self._writable()
            before = self._stat(source, internal=source_internal)
            self._expect(before, expected_revision)
            if before.kind == "symlink":
                raise FilesystemError("UNSUPPORTED_ENTRY", "Symlink mutations are not supported")
            src_parent, _, src_name = source.rpartition("/")
            dst_parent, _, dst_name = destination.rpartition("/")
            with self._open(src_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=source_internal) as src_fd:
                with self._open(dst_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=destination_internal) as dst_fd:
                    self._assert_parent(src_parent, src_fd, internal=source_internal)
                    self._assert_parent(dst_parent, dst_fd, internal=destination_internal)
                    self._expect(self._stat(source, internal=source_internal), before.revision)
                    self.volume.check()
                    _renameat2(src_fd, src_name, dst_fd, dst_name)
                    # Rename may have committed when a later flush/check fails;
                    # callers must reconcile a durable intent against identities.
                    os.fsync(src_fd)
                    if src_fd != dst_fd:
                        os.fsync(dst_fd)
                    self._assert_parent(src_parent, src_fd, internal=source_internal)
                    self._assert_parent(dst_parent, dst_fd, internal=destination_internal)
            result = self._stat(destination, internal=destination_internal)
            if (before.device, before.inode) != (result.device, result.inode):
                raise FilesystemError("STALE_REVISION", "Source identity changed at publication")
            return result

    def rename_noreplace(self, source: str, destination: str, expected_revision: int | None = None) -> Entry:
        return self._rename(source, destination, expected_revision)

    def _unlink(self, path: str, expected_revision: int | None = None, *, directory: bool = False, internal: bool = False) -> None:
        validate_path(path, allow_root=False, internal=internal)
        with self._lock:
            self._writable()
            before = self._stat(path, internal=internal)
            self._expect(before, expected_revision)
            if before.kind == "symlink":
                raise FilesystemError("UNSUPPORTED_ENTRY", "Symlink mutations are not supported")
            if directory != (before.kind == "directory"):
                raise FilesystemError("UNSUPPORTED_ENTRY", "Explicit directory removal is required")
            parent, _, name = path.rpartition("/")
            with self._open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=internal) as fd:
                self._assert_parent(parent, fd, internal=internal)
                self._expect(self._stat(path, internal=internal), before.revision)
                self.volume.check()
                try:
                    (os.rmdir if directory else os.unlink)(name, dir_fd=fd)
                    os.fsync(fd)
                except OSError as exc:
                    _raise_os(exc.errno, "Entry removal")
                self._assert_parent(parent, fd, internal=internal)

    def unlink(self, path: str, expected_revision: int | None = None, *, directory: bool = False) -> None:
        """Low-level deletion; ordinary user deletion must go through Trash."""
        self._unlink(path, expected_revision, directory=directory)

    def _internal_path(self, area: str, token: str, relative_path: str = "") -> str:
        if area not in _INTERNAL_AREAS or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", token):
            raise FilesystemError("INVALID_PATH", "Invalid protected-area reference")
        validate_path(relative_path)
        return f"{INTERNAL_ROOT}/{area}/{token}" + ("/" + relative_path if relative_path else "")

    def _ensure_area(self, area: str) -> None:
        if area not in _INTERNAL_AREAS:
            raise FilesystemError("INVALID_PATH", "Unknown internal area")
        self._writable()
        for path in (INTERNAL_ROOT, f"{INTERNAL_ROOT}/{area}"):
            try:
                self._create_dir(path, internal=True)
            except FilesystemError as exc:
                if exc.code != "CONFLICT":
                    raise
            with self._open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=True) as fd:
                info = os.fstat(fd)
                if info.st_uid != os.geteuid() or statmod.S_IMODE(info.st_mode) != 0o700:
                    raise FilesystemError("PERMISSION_DENIED", "Internal directory ownership or permissions are unsafe")

    def internal_stat(self, area: str, token: str, relative_path: str = "") -> Entry:
        return self._stat(self._internal_path(area, token, relative_path), internal=True)

    def iter_internal(self, area: str, token: str, relative_path: str = "") -> Iterator[Entry]:
        yield from self._iter_entries(self._internal_path(area, token, relative_path), internal=True)

    def read_internal(self, area: str, token: str, relative_path: str = "", *, expected_revision: int | None = None) -> CheckedFile:
        return self._open_file(self._internal_path(area, token, relative_path), internal=True, expected_revision=expected_revision)

    def open_internal_file(self, area: str, token: str, relative_path: str = "", *, writable: bool = True) -> CheckedFile:
        """Reopen an existing protected regular file; never creates or truncates it."""
        return self._open_file(self._internal_path(area, token, relative_path), writable=writable, internal=True)

    def create_internal_dir(self, area: str, token: str, relative_path: str = "") -> Entry:
        path = self._internal_path(area, token, relative_path)
        self._ensure_area(area)
        return self._create_dir(path, internal=True)

    def create_internal_file(self, area: str, token: str, relative_path: str = "") -> CheckedFile:
        path = self._internal_path(area, token, relative_path)
        self._ensure_area(area)
        return self._open_file(path, writable=True, create=True, internal=True)

    def create_staging(self, token: str) -> CheckedFile:
        return self.create_internal_file("staging", token)

    def open_staging(self, token: str) -> CheckedFile:
        return self._open_file(self._internal_path("staging", token), writable=True, internal=True)

    def _flush_tree(self, path: str, checkpoint=lambda: None) -> None:
        checkpoint()
        entry = self._stat(path, internal=True)
        if entry.kind == "directory":
            for child in self._iter_entries(path, internal=True):
                self._flush_tree(child.path, checkpoint)
            with self._open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, internal=True) as fd:
                os.fsync(fd)
        elif entry.kind == "file":
            with self._open_file(path, internal=True, expected_revision=entry.revision) as stream:
                os.fsync(stream.fileno())
                stream.check()
        else:
            raise FilesystemError("UNSUPPORTED_ENTRY", "Symlink publication is unsupported")

    def commit_internal(self, area: str, token: str, destination: str, *, checkpoint=lambda: None, publication=nullcontext) -> Entry:
        path = self._internal_path(area, token)
        self._flush_tree(path, checkpoint)
        checkpoint()
        with publication():
            return self._rename(path, destination, source_internal=True)

    def commit_staging(self, token: str, destination: str, *, checkpoint=lambda: None, publication=nullcontext) -> Entry:
        return self.commit_internal("staging", token, destination, checkpoint=checkpoint, publication=publication)

    def move_to_internal(self, path: str, area: str = "trash", token: str = "", *, expected_revision: int | None = None) -> Entry:
        destination = self._internal_path(area, token)
        self._ensure_area(area)
        return self._rename(path, destination, expected_revision, destination_internal=True)

    def restore_internal(self, area: str, token: str, destination: str) -> Entry:
        return self._rename(self._internal_path(area, token), destination, source_internal=True)

    def remove_internal(self, area: str, token: str, *, checkpoint=None) -> None:
        """Only for an authorized Trash purge/partial discard, never auto-purge."""
        def remove(path: str) -> None:
            if checkpoint is not None: checkpoint()
            entry = self._stat(path, internal=True)
            if entry.kind == "directory":
                for child in self._iter_entries(path, internal=True):
                    remove(child.path)
                entry = self._stat(path, internal=True)
            self._unlink(path, entry.revision, directory=entry.kind == "directory", internal=True)
        remove(self._internal_path(area, token))

    def discard_staging(self, token: str) -> None:
        self.remove_internal("staging", token)
