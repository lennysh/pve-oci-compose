"""Compose ``rootfs_files`` / ``mount_files`` injection into a mounted CT rootfs.

All writes go through :mod:`pve_oci_compose.guestfs` (no symlink following, no ``..``).

Ownership: ``owner`` / ``group`` are **guest** ids. For unprivileged CTs they are mapped
to host ids through the CT's ``lxc.idmap`` lines (default ``0 → 100000``, 65536 ids).
Values ``>= 65536`` are treated as already-mapped host ids (backward compatible with
older compose files that wrote e.g. ``owner: 100033``). Default owner is guest root.

CLI (used by the bash implementation; service JSON on stdin)::

    python3 -m pve_oci_compose.inject --vmid 101 --phase apply \
        --compose-dir DIR --rootfs /var/lib/lxc/101/rootfs [--unprivileged-default 1]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from .guestfs import GuestPathError, split_guest_path, write_file

GUEST_ID_RANGE = 65536


def truthy(v, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return default


def on_refresh(item: dict) -> bool:
    for k in ("on_refresh", "refresh"):
        if k in item:
            return truthy(item[k])
    return False


def should_run(item: dict, phase: str) -> bool:
    return phase == "apply" or on_refresh(item)


@dataclass
class IdMap:
    """Guest → host id ranges, per kind ('u' / 'g'). Empty = identity (privileged)."""

    ranges: dict[str, list[tuple[int, int, int]]] = field(default_factory=lambda: {"u": [], "g": []})

    def _map(self, kind: str, guest_id: int) -> int:
        rs = self.ranges[kind]
        if not rs:
            return guest_id
        if guest_id >= GUEST_ID_RANGE:
            return guest_id  # already a host id
        for gstart, hstart, count in rs:
            if gstart <= guest_id < gstart + count:
                return hstart + (guest_id - gstart)
        raise ValueError(f"guest {kind}id {guest_id} is not covered by the CT's id map")

    def uid(self, guest_id: int) -> int:
        return self._map("u", guest_id)

    def gid(self, guest_id: int) -> int:
        return self._map("g", guest_id)


def config_value(lines: Iterable[str], key: str) -> str | None:
    for line in lines:
        m = re.match(rf"^{re.escape(key)}\s*[:=]\s*(.*)$", line)
        if m:
            return m.group(1).strip()
    return None


def idmap_from_config(lines: list[str], unprivileged_default: bool = True) -> IdMap:
    """Build the id map from ``pct config`` lines (unprivileged + optional lxc.idmap).

    ``unprivileged_default`` is only used when no config lines are available."""
    unpriv_raw = config_value(lines, "unprivileged")
    if unpriv_raw is not None:
        unpriv = truthy(unpriv_raw)
    elif lines:
        unpriv = False  # readable config without unprivileged: → privileged CT (PVE default)
    else:
        unpriv = unprivileged_default  # config unavailable: fall back to compose / pct-create default
    im = IdMap()
    if not unpriv:
        return im
    for line in lines:
        m = re.match(r"^lxc\.idmap\s*[:=]\s*([ug])\s+(\d+)\s+(\d+)\s+(\d+)\s*$", line)
        if m:
            im.ranges[m.group(1)].append((int(m.group(2)), int(m.group(3)), int(m.group(4))))
    for k in ("u", "g"):
        if not im.ranges[k]:
            im.ranges[k] = [(0, 100000, GUEST_ID_RANGE)]
    return im


def parse_mp_lines(
    lines: Iterable[str], pvesm_path: Callable[[str], str | None]
) -> dict[str, tuple[str, str]]:
    """guest mount path -> (kind, host_base); kind is 'bind' or 'volume'."""
    out: dict[str, tuple[str, str]] = {}
    for line in lines:
        if not re.match(r"^mp\d+:", line):
            continue
        body = line.split(":", 1)[1].strip()
        m = re.search(r"(?:^|,)mp=([^,]+)", body)
        if not m:
            continue
        guest = m.group(1).strip().rstrip("/") or "/"
        vol = body.split(",")[0].strip()
        if vol.startswith("/"):
            out[guest] = ("bind", vol)
        else:
            out[guest] = ("volume", pvesm_path(vol) or "")
    return out


def guest_dest_for_mount_item(item: dict) -> str:
    mp = (item.get("mount") or item.get("mp") or "").strip().rstrip("/") or "/"
    rel = (item.get("path") or "").strip()
    if rel.startswith("/"):
        return rel
    return f"{mp}/{rel.lstrip('/')}" if mp != "/" else f"/{rel.lstrip('/')}"


def resolve_target(
    guest: str, rootfs: str, mps: dict[str, tuple[str, str]]
) -> tuple[str, str]:
    """(root, path-under-root) for a guest path that lives on a mount point.

    Bind mounts and directory-backed volumes are written on their host path; anything
    else (e.g. block-device volumes) goes through the pct-mounted rootfs, where
    ``pct mount`` has mounted the volume.
    """
    parts = split_guest_path(guest)
    g = "/" + "/".join(parts)
    for guest_mp, (kind, base) in sorted(mps.items(), key=lambda x: -len(x[0])):
        if g == guest_mp:
            raise GuestPathError(f"{guest}: path is the mount point itself, not a file under it")
        if g.startswith(guest_mp.rstrip("/") + "/"):
            rel = g[len(guest_mp.rstrip("/")) :]
            if base and os.path.isdir(base):
                return base, rel
            return rootfs, g
    raise GuestPathError(f"{guest}: not under any mpN mount point of this CT")


def item_bytes(item: dict, compose_dir: str) -> bytes:
    if "content" in item and item.get("content") is not None:
        c = item["content"]
        return (c if isinstance(c, str) else json.dumps(c)).encode("utf-8")
    src = str(item["source"]).strip()
    path = src if src.startswith("/") else os.path.normpath(os.path.join(compose_dir, src))
    with open(path, "rb") as f:
        return f.read()


def parse_mode(item: dict) -> int:
    m = item.get("mode")
    if m is None:
        return 0o644
    if isinstance(m, int):
        return m
    return int(str(m).strip(), 8)


def _owner(item: dict, key: str) -> int:
    v = item.get(key)
    return 0 if v is None else int(v)


def run(
    phase: str,
    doc: dict,
    compose_dir: str,
    rootfs: str,
    config_lines: list[str],
    pvesm_path: Callable[[str], str | None],
    unprivileged_default: bool = True,
) -> list[tuple[str, str]]:
    """Inject files for ``phase`` ('apply' = all, 'refresh' = on_refresh only)."""
    if phase not in ("apply", "refresh"):
        raise ValueError(f"invalid phase {phase!r}")
    idmap = idmap_from_config(config_lines, unprivileged_default)
    root_uid, root_gid = idmap.uid(0), idmap.gid(0)
    done: list[tuple[str, str]] = []
    mps: dict[str, tuple[str, str]] | None = None
    for kind, key in (("rootfs", "rootfs_files"), ("mount", "mount_files")):
        for item in doc.get(key) or []:
            if not should_run(item, phase):
                continue
            if kind == "rootfs":
                guest = str(item["path"]).strip()
                root, path = rootfs, guest
            else:
                if mps is None:
                    mps = parse_mp_lines(config_lines, pvesm_path)
                guest = guest_dest_for_mount_item(item)
                root, path = resolve_target(guest, rootfs, mps)
            write_file(
                root,
                path,
                item_bytes(item, compose_dir),
                mode=parse_mode(item),
                uid=idmap.uid(_owner(item, "owner")),
                gid=idmap.gid(_owner(item, "group")),
                dir_uid=root_uid,
                dir_gid=root_gid,
            )
            done.append((kind, guest))
    return done


def _pct_config_lines(vmid: str) -> list[str]:
    try:
        return subprocess.run(
            ["pct", "config", vmid], capture_output=True, text=True, check=True
        ).stdout.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []


def _pvesm_path(volid: str) -> str | None:
    try:
        return subprocess.run(
            ["pvesm", "path", volid], capture_output=True, text=True, check=True
        ).stdout.strip() or None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="pve_oci_compose.inject")
    ap.add_argument("--vmid", required=True)
    ap.add_argument("--phase", required=True, choices=("apply", "refresh"))
    ap.add_argument("--compose-dir", required=True)
    ap.add_argument("--rootfs", required=True)
    ap.add_argument("--unprivileged-default", default="1")
    a = ap.parse_args(argv)
    doc = json.load(sys.stdin)
    try:
        done = run(
            a.phase,
            doc,
            a.compose_dir,
            a.rootfs,
            _pct_config_lines(a.vmid),
            _pvesm_path,
            truthy(a.unprivileged_default, True),
        )
    except (GuestPathError, OSError, ValueError, KeyError) as e:
        print(f"pve-oci-compose: file inject failed: {e}", file=sys.stderr)
        return 1
    for kind, g in done:
        print(f"INJECTED\t{kind}\t{g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
