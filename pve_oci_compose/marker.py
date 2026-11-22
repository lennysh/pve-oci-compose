"""Guest marker JSON (``/etc/pve-oci-compose.json``) + sentinel tag.

The marker lives inside the CT rootfs so pct snapshots / rollback carry it. It is read
and written from the host through :mod:`guestfs` — via ``/proc/<pid>/root`` when the CT
is running (works for distroless / scratch images: no shell or ``cat`` needed in the
guest), else via a brief ``pct mount``.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import dataclass

from .errors import ComposeError
from .guestfs import GuestPathError, read_file, write_file
from .pve import MARKER_TAG, Pve, merge_marker_tag


def marker_path() -> str:
    p = os.environ.get("PVE_OCI_ROOTFS_MARKER") or "/etc/pve-oci-compose.json"
    return p if p.startswith("/") else "/" + p


@dataclass
class Marker:
    service: str | None
    ref: str | None
    digest: str | None = None  # registry digest of `ref` when it was pulled (None: older marker)


@contextmanager
def guest_root(pve: Pve, vmid: int):
    """Yield a host path to the CT's root tree (running: /proc/PID/root; stopped: pct mount)."""
    if pve.status(vmid) == "running":
        pid = pve.pid(vmid)
        if pid:
            yield f"/proc/{pid}/root"
            return
    mp = pve.mount(vmid)
    try:
        if not os.path.isdir(mp):
            raise ComposeError(f"pct mount {vmid}: rootfs not found at {mp}")
        yield mp
    finally:
        pve.unmount(vmid)


def parse(blob: bytes | None) -> Marker | None:
    if not blob:
        return None
    try:
        d = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(d, dict):
        return None
    def s(k):
        return str(d[k]) if d.get(k) is not None else None

    return Marker(s("service"), s("ref"), s("digest"))


def read(pve: Pve, vmid: int) -> Marker | None:
    """Best effort: None when the CT is not local, not mountable, or has no marker."""
    if not pve.exists_here(vmid):
        return None
    try:
        with guest_root(pve, vmid) as root:
            return parse(read_file(root, marker_path()))
    except Exception:  # noqa: BLE001 - plan/refresh treat any failure as "no marker"
        return None


def encode(service: str, ref: str, digest: str | None = None) -> bytes:
    d = {"service": service, "ref": ref}
    if digest:
        d["digest"] = digest
    return (json.dumps(d, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def write_at(root: str, service: str, ref: str, digest: str | None = None) -> None:
    try:
        write_file(root, marker_path(), encode(service, ref, digest), mode=0o644)
    except (GuestPathError, OSError) as e:
        raise ComposeError(f"could not write guest marker {marker_path()}: {e}") from e


def tag(pve: Pve, vmid: int) -> None:
    pve.mutate(["pct", "set", str(vmid), "--tags", merge_marker_tag(pve.tags(vmid))])


def is_managed(pve: Pve, vmid: int, m: Marker | None) -> bool:
    return bool(m and m.ref) or MARKER_TAG in pve.tags(vmid).replace(",", ";").split(";")
