"""Datacenter resource pools (UI grouping). Missing pools are auto-created unless
PVE_OCI_POOL_NO_AUTOCREATE=1."""

from __future__ import annotations

import os
import re

from . import ui
from .errors import ComposeError
from .pve import Pve


def ensure(pve: Pve, pool: str) -> bool:
    """Make sure ``pool`` exists. Returns True when this call created it."""
    if pve.pool_exists(pool):
        return False
    if os.environ.get("PVE_OCI_POOL_NO_AUTOCREATE") == "1":
        raise ComposeError(f"pool '{pool}' not found (PVE_OCI_POOL_NO_AUTOCREATE=1) — create it in the UI first")
    if pve.dry_run:
        ui.say(f"DRY-RUN: pvesh create /pools --poolid {pool}")
        return False
    cp = pve.run(["pvesh", "create", "/pools", "--poolid", pool, "--comment", "pve-oci-compose (auto-created)"], check=False)
    if cp.returncode != 0 and not re.search(r"already exist|duplicate", cp.stdout or "", re.I):
        raise ComposeError(f"could not create resource pool '{pool}': {(cp.stdout or '').strip()}")
    if not pve.pool_exists(pool):
        raise ComposeError(f"pool '{pool}' still not readable after create")
    created = cp.returncode == 0
    if created:
        ui.say(f"pve-oci-compose: created resource pool '{pool}'")
    return created


def ensure_member(pve: Pve, pool: str, vmid: int) -> None:
    ensure(pve, pool)
    if any(m.get("type") == "lxc" and int(m.get("vmid", -1)) == vmid for m in pve.pool_members(pool)):
        return
    pve.mutate(["pvesh", "set", f"/pools/{pool}", "--vms", str(vmid), "--allow-move", "1"])
