"""Symlink-safe file access inside a container root that is mounted on the host.

Everything here runs as root on the Proxmox host, against a tree that the guest (or
the image author) controls. A plain ``open(root + path)`` follows symlinks, so a
guest-planted ``/etc/foo -> /etc/shadow`` would make host root overwrite the
host's file. Instead, every path component is opened relative to its parent
directory fd with ``O_NOFOLLOW``, so resolution can never leave ``root``.

Also usable as a small CLI (used by the bash implementation)::

    python3 -m pve_oci_compose.guestfs write --root R --path /etc/x [--mode 644]
        [--uid N --gid N] [--dir-uid N --dir-gid N] < content
    python3 -m pve_oci_compose.guestfs read --root R --path /etc/x
"""

from __future__ import annotations

import argparse
import errno
import os
import stat
import sys

MAX_READ = 1 << 20


class GuestPathError(Exception):
    """Refused or failed guest path operation."""


def split_guest_path(path: str) -> list[str]:
    """Split a guest path into components, rejecting anything that could escape."""
    if not isinstance(path, str) or not path.strip():
        raise GuestPathError("empty guest path")
    if "\0" in path:
        raise GuestPathError(f"NUL byte in guest path: {path!r}")
    parts = [p for p in path.strip().split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise GuestPathError(f"'..' is not allowed in guest paths: {path!r}")
    if not parts:
        raise GuestPathError(f"guest path resolves to the root itself: {path!r}")
    return parts


def _open_root(root: str) -> int:
    try:
        return os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as e:
        raise GuestPathError(f"cannot open root {root!r}: {e.strerror}") from e


def _open_dirs(
    root: str,
    dir_parts: list[str],
    create: bool,
    dir_uid: int | None = None,
    dir_gid: int | None = None,
    dir_mode: int = 0o755,
) -> int:
    """Walk ``dir_parts`` under ``root`` without following symlinks. Returns a dir fd."""
    fd = _open_root(root)
    try:
        for name in dir_parts:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            try:
                nfd = os.open(name, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(name, dir_mode, dir_fd=fd)
                nfd = os.open(name, flags, dir_fd=fd)
                if dir_uid is not None and dir_gid is not None:
                    os.fchown(nfd, dir_uid, dir_gid)
            except OSError as e:
                if e.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise GuestPathError(
                        f"refusing to traverse {name!r}: symlink or not a directory"
                    ) from e
                raise
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def write_file(
    root: str,
    guest_path: str,
    data: bytes,
    mode: int = 0o644,
    uid: int | None = None,
    gid: int | None = None,
    dir_uid: int | None = None,
    dir_gid: int | None = None,
) -> None:
    """Create or replace a regular file at ``guest_path`` inside ``root``.

    Refuses if any component is a symlink, if the target is not a regular file,
    or if the target has more than one hard link (could alias a file elsewhere).
    Missing parent directories are created (owned by ``dir_uid``/``dir_gid``).
    """
    parts = split_guest_path(guest_path)
    try:
        dfd = _open_dirs(root, parts[:-1], create=True, dir_uid=dir_uid, dir_gid=dir_gid)
    except FileNotFoundError as e:
        raise GuestPathError(f"{guest_path}: {e.strerror}") from e
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        try:
            fd = os.open(parts[-1], flags, mode & 0o7777, dir_fd=dfd)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise GuestPathError(f"refusing to write {guest_path}: target is a symlink") from e
            raise GuestPathError(f"cannot open {guest_path}: {e.strerror}") from e
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise GuestPathError(f"refusing to write {guest_path}: not a regular file")
            if st.st_nlink > 1:
                raise GuestPathError(f"refusing to write {guest_path}: file has multiple hard links")
            os.set_blocking(fd, True)
            os.ftruncate(fd, 0)
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            if uid is not None and gid is not None:
                os.fchown(fd, uid, gid)
            os.fchmod(fd, mode & 0o7777)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def read_file(root: str, guest_path: str, max_bytes: int = MAX_READ) -> bytes | None:
    """Read a regular file inside ``root`` without following symlinks. None if absent/refused."""
    try:
        parts = split_guest_path(guest_path)
        dfd = _open_dirs(root, parts[:-1], create=False)
    except (GuestPathError, OSError):
        return None
    try:
        try:
            fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dfd
            )
        except OSError:
            return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None
            return os.read(fd, max_bytes)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


def _parse_mode(s: str) -> int:
    return int(s, 8)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pve_oci_compose.guestfs")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write")
    w.add_argument("--root", required=True)
    w.add_argument("--path", required=True)
    w.add_argument("--mode", type=_parse_mode, default=0o644)
    w.add_argument("--uid", type=int)
    w.add_argument("--gid", type=int)
    w.add_argument("--dir-uid", type=int)
    w.add_argument("--dir-gid", type=int)
    r = sub.add_parser("read")
    r.add_argument("--root", required=True)
    r.add_argument("--path", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "write":
            write_file(
                a.root, a.path, sys.stdin.buffer.read(), a.mode, a.uid, a.gid, a.dir_uid, a.dir_gid
            )
            return 0
        data = read_file(a.root, a.path)
        if data is None:
            return 1
        sys.stdout.buffer.write(data)
        return 0
    except GuestPathError as e:
        print(f"pve-oci-compose: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
