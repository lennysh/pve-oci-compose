"""Thin wrapper around the Proxmox CLI tools (pct, pvesh, pvecm, perl PVE::Storage, skopeo).

Every external call goes through :class:`Pve`, so workflows can be tested against a fake.
Mutating calls go through :meth:`Pve.mutate`, which only prints them in dry-run mode.
"""

from __future__ import annotations

import hashlib
import json
import re
import socket
import subprocess
import time
import urllib.parse
from dataclasses import dataclass

from . import ui
from .errors import CommandError, ComposeError

MARKER_TAG = "pve-oci-compose"
OCI_PULL_STORAGE_TYPES = ("dir", "nfs", "cifs")


def unwrap(doc):
    """pvesh JSON is usually bare; tolerate an API-style {"data": …} envelope."""
    if isinstance(doc, dict) and "data" in doc and len(doc) <= 2:
        return doc["data"]
    return doc


def parse_upid(output: str) -> str | None:
    m = re.findall(r"UPID:[^\"\s]+", output or "")
    if m:
        return m[-1].rstrip(",")
    try:
        v = unwrap(json.loads(output))
    except (json.JSONDecodeError, TypeError):
        return None
    return v if isinstance(v, str) and v.startswith("UPID:") else None


def split_tags(s: str | None) -> list[str]:
    return [t.strip() for t in re.split(r"[;,\s]+", s or "") if t.strip()]


def merge_marker_tag(existing: str | None) -> str:
    parts = [t for t in split_tags(existing) if t != MARKER_TAG]
    parts.append(MARKER_TAG)
    return ";".join(parts)


def parse_config(lines: list[str]) -> dict[str, str]:
    """``pct config`` lines → {key: value} for the current config (stops at [snapshot])."""
    out: dict[str, str] = {}
    for line in lines:
        if line.startswith("["):
            break
        m = re.match(r"^([A-Za-z0-9_.\-]+):\s?(.*)$", line)
        if m and m.group(1) not in out:
            out[m.group(1)] = m.group(2)
    return out


@dataclass
class Pve:
    dry_run: bool = False
    _node: str | None = None

    # ---- process helpers -------------------------------------------------
    def run(
        self,
        argv: list[str],
        check: bool = True,
        stream: bool = False,
        input_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        ui.cmd(argv)
        try:
            if stream:
                cp = subprocess.run(argv, text=True, input=input_text)
            else:
                cp = subprocess.run(
                    argv,
                    text=True,
                    input=input_text,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )
        except FileNotFoundError as e:
            raise ComposeError(f"{argv[0]} not found (run on a Proxmox VE node as root)") from e
        if check and cp.returncode != 0:
            raise CommandError(argv, cp.returncode, cp.stdout or "")
        return cp

    def ok(self, argv: list[str]) -> bool:
        return self.run(argv, check=False).returncode == 0

    def out(self, argv: list[str]) -> str | None:
        """stdout of a read-only command, or None if it failed."""
        try:
            cp = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            return None
        return cp.stdout if cp.returncode == 0 else None

    def mutate(self, argv: list[str], stream: bool = False, check: bool = True):
        """Run a state-changing command, or just print it under --dry-run."""
        if self.dry_run:
            ui.say(f"DRY-RUN: {ui.redact(argv)}")
            return None
        return self.run(argv, check=check, stream=stream)

    def pvesh_get(self, path: str, *params: str):
        raw = self.out(["pvesh", "get", path, *params, "--output-format", "json"])
        if raw is None:
            return None
        try:
            return unwrap(json.loads(raw))
        except json.JSONDecodeError:
            return None

    # ---- node / cluster ----------------------------------------------------
    def node_name(self) -> str:
        if self._node is None:
            n = (self.out(["pvecm", "nodename"]) or "").strip()
            self._node = n or socket.gethostname().split(".")[0]
        return self._node

    def nextid(self) -> int:
        v = self.pvesh_get("/cluster/nextid")
        if isinstance(v, (int, str)) and str(v).isdigit():
            return int(v)
        raise ComposeError("could not read cluster nextid (pvesh get /cluster/nextid)")

    def cluster_guests(self) -> list[dict] | None:
        v = self.pvesh_get("/cluster/resources", "--type", "vm")
        if not isinstance(v, list):
            return None
        return [g for g in v if isinstance(g, dict) and g.get("type") in ("lxc", "qemu")]

    def vmid_kind(self, vmid: int) -> str:
        """free | lxc | qemu | unknown — LXC and QEMU share one vmid namespace."""
        guests = self.cluster_guests()
        if guests is None:
            return "unknown"
        for g in guests:
            if int(g.get("vmid", -1)) == vmid:
                return g["type"]
        return "free"

    def lxc_node(self, vmid: int) -> str | None:
        for g in self.cluster_guests() or []:
            if g.get("type") == "lxc" and int(g.get("vmid", -1)) == vmid:
                return g.get("node")
        return None

    # ---- containers ----------------------------------------------------------
    def config_lines(self, vmid: int) -> list[str] | None:
        raw = self.out(["pct", "config", str(vmid)])
        return raw.splitlines() if raw is not None else None

    def config(self, vmid: int) -> dict[str, str] | None:
        lines = self.config_lines(vmid)
        return parse_config(lines) if lines is not None else None

    def api_config(self, node: str, vmid: int) -> dict | None:
        v = self.pvesh_get(f"/nodes/{node}/lxc/{vmid}/config")
        return v if isinstance(v, dict) else None

    def exists_here(self, vmid: int) -> bool:
        return self.config_lines(vmid) is not None

    def tags(self, vmid: int) -> str:
        cfg = self.config(vmid)
        if cfg and cfg.get("tags", "").strip():
            return cfg["tags"].strip()
        node = self.lxc_node(vmid)
        if node:
            acfg = self.api_config(node, vmid) or {}
            return str(acfg.get("tags") or "")
        return ""

    def status(self, vmid: int) -> str:
        raw = self.out(["pct", "status", str(vmid)]) or ""
        return raw.replace("status:", "").strip()

    def is_active(self, vmid: int) -> bool:
        return self.status(vmid) in ("running", "frozen")

    def pid(self, vmid: int) -> int | None:
        raw = (self.out(["lxc-info", "-n", str(vmid), "-p", "-H"]) or "").strip()
        return int(raw) if raw.isdigit() else None

    @staticmethod
    def rootfs_mountpoint(vmid: int) -> str:
        return f"/var/lib/lxc/{vmid}/rootfs"

    def mount(self, vmid: int) -> str:
        self.run(["pct", "mount", str(vmid)])
        return self.rootfs_mountpoint(vmid)

    def unmount(self, vmid: int) -> None:
        self.run(["pct", "unmount", str(vmid)], check=False)

    def shutdown_or_stop(self, vmid: int, timeout: int) -> bool:
        if not self.is_active(vmid):
            return True
        if self.run(["pct", "shutdown", str(vmid), "--timeout", str(timeout)], check=False, stream=True).returncode != 0:
            if self.is_active(vmid):
                self.run(["pct", "stop", str(vmid)], check=False, stream=True)
        if self.is_active(vmid):
            self.run(["pct", "stop", str(vmid)], check=False, stream=True)
        return not self.is_active(vmid)

    def destroy(self, vmid: int) -> bool:
        if self.ok(["pct", "destroy", str(vmid), "--purge"]):
            return True
        return self.ok(["pct", "destroy", str(vmid)])

    # ---- tasks ---------------------------------------------------------------
    def wait_task(self, upid: str, node: str | None = None, max_wait: int = 7200) -> None:
        node = node or self.node_name()
        quoted = urllib.parse.quote(upid, safe="")
        waited = 0
        while waited < max_wait:
            st = self.pvesh_get(f"/nodes/{node}/tasks/{quoted}/status")
            if st is None:
                st = self.pvesh_get(f"/nodes/{node}/tasks/{upid}/status")
            if isinstance(st, dict) and st.get("status") == "stopped":
                es = str(st.get("exitstatus") or "unknown")
                if es.upper() == "OK":
                    return
                hint = ""
                if "skopeo" in es and "failed" in es or "manifest unknown" in es:
                    hint = " (hint: the image tag may not exist on the registry)"
                elif "max" in es and "backup" in es and "limit" in es:
                    hint = " (hint: backup storage 'Max Backups' reached for this CT)"
                raise ComposeError(f"task failed: exitstatus={es}{hint} UPID={upid}")
            if waited == 30 and st is None:
                ui.warn(f"still waiting on task {upid} (no status from pvesh yet)")
            time.sleep(2)
            waited += 2
        raise ComposeError(f"timed out waiting for task {upid} ({max_wait}s)")

    # ---- pools -----------------------------------------------------------------
    def pool_exists(self, pool: str) -> bool:
        return isinstance(self.pvesh_get(f"/pools/{urllib.parse.quote(pool, safe='')}"), (dict, list))

    def pool_members(self, pool: str) -> list[dict]:
        v = self.pvesh_get(f"/pools/{urllib.parse.quote(pool, safe='')}")
        if isinstance(v, dict):
            return list(v.get("members") or [])
        if isinstance(v, list) and v and isinstance(v[0], dict):  # PVE 9 GET /pools?poolid=
            return list(v[0].get("members") or [])
        return []

    # ---- storage / templates -----------------------------------------------------
    def storages(self, node: str) -> list[dict]:
        v = self.pvesh_get(f"/nodes/{node}/storage")
        if not isinstance(v, list):
            raise ComposeError(f"pvesh get /nodes/{node}/storage failed")
        return v

    def storage_has_volid(self, node: str, storage: str, volid: str) -> bool:
        v = self.pvesh_get(f"/nodes/{node}/storage/{storage}/content")
        return isinstance(v, list) and any(i.get("volid") == volid for i in v if isinstance(i, dict))

    def normalize_content_filename(self, ref: str) -> str:
        out = self.out(
            ["perl", "-MPVE::Storage", "-e", "print PVE::Storage::normalize_content_filename($ARGV[0])", ref]
        )
        if not out:
            raise ComposeError("perl PVE::Storage::normalize_content_filename failed (not a PVE node?)")
        return out.strip()

    def vztmpl_dir(self, storage: str) -> str | None:
        out = self.out(
            [
                "perl",
                "-MPVE::Storage",
                "-e",
                "print PVE::Storage::get_vztmpl_dir(PVE::Storage::config(), $ARGV[0])",
                storage,
            ]
        )
        return out.strip() if out and out.strip() else None

    def skopeo_inspect(self, ref: str) -> dict | None:
        raw = self.out(["skopeo", "inspect", f"docker://{ref}"])
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def registry_digest(self, ref: str) -> str | None:
        """Current manifest digest of a (normalized) registry ref, or None if unreachable.

        sha256 of the raw top-level manifest (multi-arch index or single manifest) — the
        same value the registry reports as the tag's digest. One manifest GET, no blobs.
        """
        if "@sha256:" in ref:
            return ref.split("@", 1)[1]
        try:
            cp = subprocess.run(
                ["skopeo", "inspect", "--raw", f"docker://{ref}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            return None
        if cp.returncode != 0 or not cp.stdout:
            return None
        return "sha256:" + hashlib.sha256(cp.stdout).hexdigest()

    def pvesm_path(self, volid: str) -> str | None:
        out = self.out(["pvesm", "path", volid])
        return out.strip() if out and out.strip() else None
