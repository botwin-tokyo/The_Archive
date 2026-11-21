"""Verified Linux mounts. No function in this module creates a configured root.

Production configuration is deliberately explicit and tied to a block-device UUID.
The fixture factory is the only way to use ordinary disposable /tmp directories.
Mount identity and pinned root identity are checked at every I/O checkpoint. Once
invalidated a volume stays invalid until the owner builds a new registry; it never
silently follows a replacement disk. Managed trees require exclusive OS ownership.
"""
from __future__ import annotations

from .contracts import BackendError

from dataclasses import dataclass
from functools import lru_cache
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from typing import Callable, Iterable, Mapping


class VolumeError(BackendError):
    def __init__(self, code: str, message: str):
        super().__init__(code, message)


@dataclass(frozen=True)
class VolumeConfig:
    volume_id: str
    label: str
    root_path: str
    mount_point: str
    filesystem_uuid: str
    expected_filesystem: str
    expected_device: tuple[int, int]
    expected_mount_source: str
    expected_mount_id: int | None = None
    expected_mount_root: str = "/"


@dataclass(frozen=True)
class MountInfo:
    mount_id: int
    parent_id: int
    device: tuple[int, int]
    root: str
    mount_point: str
    options: frozenset[str]
    filesystem: str
    source: str
    super_options: frozenset[str]

    @property
    def identity(self) -> tuple:
        return (self.mount_id, self.parent_id, self.device, self.root,
                self.mount_point, self.filesystem, self.source)


def _unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)


def parse_mountinfo(content: str) -> list[MountInfo]:
    """Parse proc mountinfo including octal-escaped paths and optional fields."""
    # Keep the public list independent from the shared immutable cache.
    return list(_parsed_mountinfo(content))


@lru_cache(maxsize=8)
def _parsed_mountinfo(content: str) -> tuple[MountInfo, ...]:
    # Key by exact freshly-read kernel contents, never by path or time. Both
    # namespace and backing-mount changes must be visible on the very next check.
    result = []
    for line in content.splitlines():
        try:
            before, after = line.split(" - ", 1)
            left, right = before.split(), after.split()
            major, minor = (int(part) for part in left[2].split(":"))
            result.append(MountInfo(int(left[0]), int(left[1]), (major, minor),
                _unescape_mount(left[3]), _unescape_mount(left[4]),
                frozenset(left[5].split(",")), right[0],
                _unescape_mount(right[1]), frozenset(right[2].split(","))))
        except (ValueError, IndexError) as exc:
            raise VolumeError("DRIVE_UNAVAILABLE", "Mount metadata is malformed") from exc
    return tuple(result)


def _mountinfo() -> str:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as source:
            return source.read()
    except OSError as exc:
        raise VolumeError("DRIVE_UNAVAILABLE", "Mount metadata is unavailable") from exc


def _host_mountinfo() -> str:
    # systemd recreates the service's private mount namespace on every restart.
    # PID 1's backing mount IDs survive that recreation, but not a real remount.
    try:
        with open('/proc/1/mountinfo', encoding='utf-8') as source:
            return source.read()
    except OSError as exc:
        raise VolumeError('DRIVE_UNAVAILABLE', 'Backing mount metadata is unavailable') from exc


def _boot_identity() -> str:
    """Bind persisted sessions to this boot as mount IDs may be reused after reboot."""
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as source:
            boot_id = source.read(80).strip()
    except OSError as exc:
        raise VolumeError("DRIVE_UNAVAILABLE", "Kernel boot identity is unavailable") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", boot_id):
        raise VolumeError("DRIVE_UNAVAILABLE", "Kernel boot identity is malformed")
    return boot_id


def _uuid_device(uuid: str) -> tuple[int, int]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", uuid):
        raise VolumeError("INVALID_ARGUMENT", "Invalid filesystem UUID")
    try:
        info = os.stat("/dev/disk/by-uuid/" + uuid)
    except OSError as exc:
        raise VolumeError("DRIVE_UNAVAILABLE", "Configured filesystem UUID is unavailable") from exc
    if not stat.S_ISBLK(info.st_mode):
        raise VolumeError("VOLUME_MISMATCH", "Filesystem UUID is not a block device")
    return os.major(info.st_rdev), os.minor(info.st_rdev)


def _absolute_path(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("/") or "\0" in value:
        raise VolumeError("INVALID_ARGUMENT", "Volume paths must be absolute")
    if value != "/" and (value.endswith("/") or any(p in ("", ".", "..") for p in value[1:].split("/"))):
        raise VolumeError("INVALID_ARGUMENT", "Volume paths must be canonical without symlinks")
    return value


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def _open_root(path: str) -> int:
    # Crossing mounts is required to reach the configured disk. All symlinks,
    # including ancestors, are denied; the resulting mount/device is verified.
    from .filesystem import openat2, RESOLVE_BENEATH, RESOLVE_NO_SYMLINKS
    top = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        return openat2(top, path.lstrip("/") or ".",
                       os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC,
                       resolve=RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS)
    finally:
        os.close(top)


class VerifiedVolume:
    def __init__(self, config: VolumeConfig, *, mountinfo_reader: Callable[[], str],
                 uuid_resolver: Callable[[str], tuple[int, int]], fixture: bool = False,
                 generation_mountinfo_reader: Callable[[], str] | None = None):
        self.config = config
        self.volume_id, self.label = config.volume_id, config.label
        self._read_mountinfo, self._resolve_uuid = mountinfo_reader, uuid_resolver
        self._fixture, self._lock, self._fd = fixture, threading.RLock(), -1
        self._read_generation_mountinfo = generation_mountinfo_reader or (_host_mountinfo if not fixture and mountinfo_reader is _mountinfo else mountinfo_reader)
        self._generation, self._valid = 1, False
        self._mount = self._inspect_mount()
        self._generation_mount = self._inspect_mount(self._read_generation_mountinfo, check_mount_id=False)
        self._boot_id = _boot_identity()
        try:
            self._fd = _open_root(config.root_path)
            root = os.fstat(self._fd)
            self._root_identity = (root.st_dev, root.st_ino)
            self._verify_root(root)
            if self._inspect_mount().identity != self._mount.identity:
                raise VolumeError("DRIVE_UNAVAILABLE", "Volume changed while opening its root")
            # Deterministic across service recreation on the same verified mount,
            # different across remount/root replacement/reboot. A boot identity is
            # essential because Linux may reuse mount IDs on a subsequent boot.
            identity = (self._boot_id, config.volume_id, config.filesystem_uuid,
                        config.root_path, self._generation_mount.identity, self._root_identity)
            self._generation = int.from_bytes(hashlib.sha256(repr(identity).encode("utf-8")).digest()[:8], "big") & ((1 << 53) - 1)
            self._generation = self._generation or 1
            self._valid = True
        except Exception as exc:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            if isinstance(exc, BackendError) and exc.code not in ("UNSUPPORTED_ENTRY", "VOLUME_MISMATCH", "DRIVE_UNAVAILABLE"):
                raise VolumeError("DRIVE_UNAVAILABLE", "Configured root is unavailable") from exc
            raise

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def root_path(self) -> str:
        """Administrative configuration only. Never use this path for file I/O."""
        return self.config.root_path

    def _inspect_mount(self, reader=None, *, check_mount_id=True) -> MountInfo:
        cfg = self.config
        mounts = _parsed_mountinfo((reader or self._read_mountinfo)())
        if self._fixture:
            candidates = [m for m in mounts if _under(cfg.root_path, m.mount_point)]
            if not candidates:
                raise VolumeError("DRIVE_UNAVAILABLE", "Fixture mount is unavailable")
            mounted = max(candidates, key=lambda m: (len(m.mount_point), m.mount_id))
        else:
            candidates = [m for m in mounts if m.mount_point == cfg.mount_point]
            if len(candidates) != 1:
                raise VolumeError("DRIVE_UNAVAILABLE", "Expected mount is absent or ambiguous")
            mounted = candidates[0]
            # A nested mount at/before the managed root cannot substitute identity.
            covering = [m for m in mounts if _under(cfg.root_path, m.mount_point)]
            if max(covering, key=lambda m: (len(m.mount_point), m.mount_id)) != mounted:
                raise VolumeError("VOLUME_MISMATCH", "Unexpected nested root mount")
            if self._resolve_uuid(cfg.filesystem_uuid) != cfg.expected_device:
                raise VolumeError("VOLUME_MISMATCH", "Filesystem UUID changed")
        if (mounted.device != cfg.expected_device or mounted.filesystem != cfg.expected_filesystem
                or mounted.source != cfg.expected_mount_source or mounted.root != cfg.expected_mount_root
                or (check_mount_id and cfg.expected_mount_id is not None and mounted.mount_id != cfg.expected_mount_id)):
            raise VolumeError("VOLUME_MISMATCH", "Mounted filesystem does not match configuration")
        return mounted

    def _verify_root(self, info: os.stat_result) -> None:
        if not stat.S_ISDIR(info.st_mode) or (os.major(info.st_dev), os.minor(info.st_dev)) != self.config.expected_device:
            raise VolumeError("VOLUME_MISMATCH", "Root does not belong to the expected filesystem")
        if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise VolumeError("PERMISSION_DENIED", "Managed root must be service-owned and not writable by other accounts")

    def check(self, generation: int | None = None) -> None:
        """Fail closed and invalidate all checkpointed handles after any change."""
        with self._lock:
            if not self._valid or self._fd < 0:
                raise VolumeError("DRIVE_UNAVAILABLE", "Volume is invalidated or closed")
            if generation is not None and generation != self._generation:
                raise VolumeError("DRIVE_UNAVAILABLE", "Volume generation is stale")
            fresh = -1
            try:
                mounted = self._inspect_mount()
                if mounted.identity != self._mount.identity:
                    raise VolumeError("DRIVE_UNAVAILABLE", "Mount generation changed")
                fresh = _open_root(self.config.root_path)
                root = os.fstat(fresh)
                self._verify_root(root)
                if (root.st_dev, root.st_ino) != self._root_identity:
                    raise VolumeError("DRIVE_UNAVAILABLE", "Configured root was replaced")
                if (os.fstat(self._fd).st_dev, os.fstat(self._fd).st_ino) != self._root_identity:
                    raise VolumeError("DRIVE_UNAVAILABLE", "Pinned root changed")
                # Recheck after opening to catch a concurrent remount.
                if self._inspect_mount().identity != self._mount.identity:
                    raise VolumeError("DRIVE_UNAVAILABLE", "Mount changed during checkpoint")
                if self._inspect_mount(self._read_generation_mountinfo, check_mount_id=False).identity != self._generation_mount.identity:
                    raise VolumeError('DRIVE_UNAVAILABLE', 'Backing mount changed during checkpoint')
                self._mount = mounted
            except Exception as exc:
                self._valid = False
                self._generation = self._generation % ((1 << 53) - 1) + 1
                os.close(self._fd)
                self._fd = -1
                if not isinstance(exc, VolumeError) and not (isinstance(exc, BackendError) and exc.code == "UNSUPPORTED_ENTRY"):
                    raise VolumeError("DRIVE_UNAVAILABLE", "Configured root failed identity revalidation") from exc
                raise
            finally:
                if fresh >= 0:
                    os.close(fresh)

    def open_root(self) -> int:
        with self._lock:
            self.check()
            return os.dup(self._fd)

    def capacity(self) -> dict:
        with self._lock:
            self.check()
            info = os.fstatvfs(self._fd)
            readonly = bool(info.f_flag & os.ST_RDONLY) or "ro" in self._mount.options or "ro" in self._mount.super_options
            self.check()
            return {"total": info.f_blocks * info.f_frsize,
                    "free": info.f_bfree * info.f_frsize,
                    "available": info.f_bavail * info.f_frsize,
                    "read_only": readonly, "generation": self._generation}

    def close(self) -> None:
        with self._lock:
            self._valid = False
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            self._generation = self._generation % ((1 << 53) - 1) + 1


class VolumeRegistry:
    def __init__(self, configs: Iterable[VolumeConfig] = (), *,
                 mountinfo_reader: Callable[[], str] = _mountinfo,
                 uuid_resolver: Callable[[str], tuple[int, int]] = _uuid_device):
        self._volumes: dict[str, VerifiedVolume] = {}
        devices = set()
        try:
            for cfg in configs:
                self._validate_config(cfg)
                if cfg.volume_id in self._volumes or cfg.expected_device in devices:
                    raise VolumeError("INVALID_ARGUMENT", "Volume IDs and independent devices must be unique")
                self._volumes[cfg.volume_id] = VerifiedVolume(cfg, mountinfo_reader=mountinfo_reader, uuid_resolver=uuid_resolver)
                devices.add(cfg.expected_device)
        except Exception:
            self.close()
            raise

    @staticmethod
    def _validate_config(cfg: VolumeConfig) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", cfg.volume_id):
            raise VolumeError("INVALID_ARGUMENT", "Invalid volume ID")
        _absolute_path(cfg.root_path)
        _absolute_path(cfg.mount_point)
        _absolute_path(cfg.expected_mount_root)
        if cfg.mount_point == "/" or not _under(cfg.root_path, cfg.mount_point):
            raise VolumeError("INVALID_ARGUMENT", "A dedicated mount containing the root is required")
        if not cfg.filesystem_uuid or not cfg.expected_filesystem or not cfg.expected_mount_source:
            raise VolumeError("INVALID_ARGUMENT", "Stable mount identity must be fully configured")
        if len(cfg.expected_device) != 2 or any(type(n) is not int or n < 0 for n in cfg.expected_device):
            raise VolumeError("INVALID_ARGUMENT", "Invalid device major/minor")

    @classmethod
    def fixtures(cls, roots: Mapping[str, str | Path]) -> VolumeRegistry:
        """Explicit TEST-ONLY factory for pre-existing, owned disposable tempdirs.

        It intentionally permits multiple fixture volumes on the same host device.
        No production loader should expose this factory or accept a fixture flag.
        """
        registry = cls()
        temp_root = os.path.realpath(tempfile.gettempdir())
        mounts = parse_mountinfo(_mountinfo())
        used: list[str] = []
        try:
            for volume_id, supplied in roots.items():
                path = _absolute_path(os.fspath(supplied))
                if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", volume_id):
                    raise VolumeError("INVALID_ARGUMENT", "Invalid fixture volume ID")
                if path == temp_root or not _under(path, temp_root):
                    raise VolumeError("INVALID_ARGUMENT", "Fixtures require disposable directories beneath the temp directory")
                if any(_under(path, previous) or _under(previous, path) for previous in used):
                    raise VolumeError("INVALID_ARGUMENT", "Fixture roots must not overlap")
                root = os.stat(path, follow_symlinks=False)
                if not stat.S_ISDIR(root.st_mode) or root.st_uid != os.geteuid():
                    raise VolumeError("INVALID_ARGUMENT", "Fixture root must be an existing owned directory")
                covering = max((m for m in mounts if _under(path, m.mount_point)), key=lambda m: (len(m.mount_point), m.mount_id))
                cfg = VolumeConfig(volume_id, volume_id, path, covering.mount_point, "fixture-only",
                    covering.filesystem, covering.device, covering.source, covering.mount_id, covering.root)
                registry._volumes[volume_id] = VerifiedVolume(cfg, mountinfo_reader=_mountinfo, uuid_resolver=_uuid_device, fixture=True)
                used.append(path)
            return registry
        except Exception:
            registry.close()
            raise

    def get(self, volume_id: str) -> VerifiedVolume:
        try:
            return self._volumes[volume_id]
        except KeyError as exc:
            raise VolumeError("DRIVE_UNAVAILABLE", "Volume is not configured") from exc

    def list(self) -> list[VerifiedVolume]:
        return list(self._volumes.values())

    def close(self) -> None:
        for volume in self._volumes.values():
            volume.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
