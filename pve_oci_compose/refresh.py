"""Rootfs refresh: replace a CT's root filesystem with a newer image, keeping the CTID,
host config, mount points and volumes.

    temp CT from the new image (target still running) → shutdown → pre-backup
    (snapshot / vzdump) → mount both → rsync --delete (mount points excluded) →
    marker + on_refresh files → sync entrypoint + env → destroy temp → start if it was running

Downtime covers only shutdown → start: the registry pull and OCI unpack happen first,
so a bad tag, unsupported image or full storage fails without touching the target.

Failure handling: before rsync starts, the temp CT is removed and the target restarted
if it was running. Once rsync has started the rootfs may be half-replaced, so the CT is
rolled back to the pre-refresh snapshot when one exists (OCI_REFRESH_NO_AUTO_ROLLBACK=1
disables that) — otherwise it is left stopped with recovery instructions.
"""

from __future__ import annotations

import os
import re
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass

from . import ctconf, refs, ui
from .create import CreateSpec, Template, create_ct, pull, resolve_template
from .errors import ComposeError
from .pve import TEMP_TAG, Pve, parse_config, parse_upid


@dataclass
class RefreshOptions:
    pre_backup: str = "snapshot"  # snapshot | auto | vzdump | none
    vzdump_storage: str | None = None
    allow_failed_backup: bool = False
    vzdump_remove: bool = False
    vzdump_protected: bool = True
    template_storage: str | None = None
    temp_vmid: int | None = None
    shutdown_timeout: int = int(os.environ.get("OCI_REFRESH_SHUTDOWN_TIMEOUT") or 60)
    auto_rollback: bool = os.environ.get("OCI_REFRESH_NO_AUTO_ROLLBACK") != "1"


def rootfs_alloc(rootfs_line: str) -> str:
    """``local-zfs:subvol-100-disk-0,size=8G`` → ``local-zfs:8`` (new-volume syntax, GiB)."""
    storage = rootfs_line.split(":", 1)[0]
    m = re.search(r"(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)", rootfs_line)
    if not storage or not m:
        raise ComposeError(f"could not parse storage/size from rootfs line: {rootfs_line}")
    n, unit = float(m.group(1)), m.group(2) or "G"
    gib = {"K": n / 1024**2, "M": n / 1024, "G": n, "T": n * 1024}[unit]
    return f"{storage}:{max(1, int(-(-gib // 1)))}"


def rsync_excludes(config_lines: list[str]) -> list[str]:
    """Anchored ``--exclude=/path`` for every mpN guest path (never descend into volumes)."""
    out = []
    for line in config_lines:
        if line.startswith("["):
            break
        if not re.match(r"^mp\d+:", line):
            continue
        m = re.search(r"(?:^|[ ,])mp=([^,]+)", line)
        if m:
            rel = m.group(1).strip().strip("/")
            if rel:
                out.append(f"--exclude=/{rel}")
    return out


def _vzdump(pve: Pve, vmid: int, opts: RefreshOptions, hostname: str) -> None:
    node = pve.node_name()
    argv = ["pvesh", "create", f"/nodes/{node}/vzdump", "--vmid", str(vmid), "--storage", str(opts.vzdump_storage),
            "--mode", "stop", "--compress", "zstd"]
    argv += ["--remove", "1"] if opts.vzdump_remove else ["--remove", "0", "--prune-backups", "keep-all=1"]
    if opts.vzdump_protected:
        argv += ["--protected", "1"]
    argv += ["--notes-template", f"{hostname} - pve-oci-compose-pre-refresh", "--output-format", "json"]
    ui.sub(f"vzdump CT {vmid} → {opts.vzdump_storage}")
    cp = pve.run(argv, check=False)
    if cp.returncode != 0:
        raise ComposeError(f"vzdump failed: {(cp.stdout or '').strip()[-400:]}")
    upid = parse_upid(cp.stdout or "")
    if upid:
        pve.wait_task(upid, node)
    ui.ok("vzdump finished")


def refresh_ct(
    pve: Pve,
    vmid: int,
    image: str,
    opts: RefreshOptions,
    on_rootfs: Callable[[str, Template | None], None] | None = None,
    env_layers: list[dict[str, str]] | None = None,
) -> str | None:
    """Replace CT ``vmid``'s rootfs with ``image``. Returns the pulled ref (registry refs)."""
    lines = pve.config_lines(vmid)
    if lines is None:
        raise ComposeError(f"no CT config for vmid {vmid} on this node")
    cfg = parse_config(lines)
    if "rootfs" not in cfg:
        raise ComposeError(f"could not read rootfs: for CT {vmid}")
    alloc = rootfs_alloc(cfg["rootfs"])
    hostname = cfg.get("hostname") or f"vmid{vmid}"
    node = pve.node_name()

    template: Template | None = None
    if refs.is_registry_ref(image):
        template = resolve_template(pve, node, image, opts.template_storage)

    temp = opts.temp_vmid or pve.nextid()
    if temp == vmid:
        raise ComposeError("temp vmid equals the CT being refreshed")

    ui.title("OCI rootfs refresh")
    ui.kv("CT (keep)", vmid)
    ui.kv("Temp CT", temp)
    ui.kv("Image", template.pull_ref if template else image)
    ui.kv("Temp alloc", alloc)
    ui.kv("Pre-backup", opts.pre_backup)

    if pve.dry_run:
        ui.say(f"DRY-RUN: pct create {temp} from {template.ostemplate if template else image}; "
               f"shutdown CT {vmid}; pre-backup={opts.pre_backup}; rsync -aHAX --delete "
               f"{' '.join(rsync_excludes(lines))} {temp} → {vmid}; destroy {temp}")
        return template.pull_ref if template else None

    was_running = pve.is_active(vmid)
    temp_created = destructive = stopped = False
    snap: str | None = None
    mounted: list[int] = []

    def unmount_all():
        for v in reversed(mounted):
            pve.unmount(v)
        mounted.clear()

    try:
        # 1. temp CT from the new image — while the target keeps running, so the
        #    download + unpack (and any failure there) costs no downtime
        args = ["--hostname", "oci-refresh-temp", "--tags", TEMP_TAG,
                "--net0", os.environ.get("OCI_REFRESH_TEMP_NET0") or "name=eth0,bridge=vmbr0,ip=dhcp",
                "--onboot", "0", "--unprivileged", cfg.get("unprivileged") or "0"]
        for k in ("ostype", "arch", "features", "memory"):
            if cfg.get(k):
                args += [f"--{k}", cfg[k]]
        spec = CreateSpec(vmid=temp, rootfs=alloc, args=args)
        temp_created = True
        if template:
            pull(pve, node, template, quiet=True)
            create_ct(pve, template, spec, quiet=True)
        else:
            create_ct(pve, Template("", image, image, image, None), spec, quiet=True)
        ui.ok(f"Temp CT {temp} ready (rsync source; will be destroyed)")

        # 2. shutdown (downtime starts here)
        ui.sub(f"Shutdown CT {vmid}")
        if was_running:
            stopped = True
            if not pve.shutdown_or_stop(vmid, opts.shutdown_timeout):
                raise ComposeError(f"could not shut down or stop CT {vmid}")
        ui.ok(f"CT {vmid} offline")

        # 3. pre-backup (on the stopped CT, right before its rootfs is touched)
        if opts.pre_backup in ("snapshot", "auto"):
            name = f"pre-oci-refresh-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}UTC"
            ui.sub(f"Pre-backup ({opts.pre_backup}) — snapshot {name}")
            cp = pve.run(["pct", "snapshot", str(vmid), name, "--description",
                          f"pve-oci-compose before refresh to {image}"], check=False)
            if cp.returncode == 0:
                snap = name
                ui.ok(f"Snapshot created (rollback: pct rollback {vmid} {name})")
            elif opts.pre_backup == "auto" and opts.vzdump_storage:
                ui.warn(f"pct snapshot failed; trying vzdump: {(cp.stdout or '').strip()[-200:]}")
                _backup_or_abort(lambda: _vzdump(pve, vmid, opts, hostname), opts)
            else:
                _abort_or_continue(f"pct snapshot failed: {(cp.stdout or '').strip()[-300:]}", opts)
        elif opts.pre_backup == "vzdump":
            if not opts.vzdump_storage:
                _abort_or_continue("pre-backup vzdump needs refresh_backup_storage / --backup-storage", opts)
            else:
                _backup_or_abort(lambda: _vzdump(pve, vmid, opts, hostname), opts)

        # 4. rsync
        ui.sub("Mount root filesystems + rsync")
        m_old = pve.mount(vmid)
        mounted.append(vmid)
        m_new = pve.mount(temp)
        mounted.append(temp)
        if not (os.path.isdir(m_old) and os.path.isdir(m_new)):
            raise ComposeError(f"mount paths missing after pct mount: {m_old} {m_new}")
        excludes = rsync_excludes(lines)
        destructive = True
        pve.run(["rsync", "-aHAX", "--delete", *excludes, f"{m_new}/", f"{m_old}/"], stream=True)
        ui.ok("rsync done")
        if on_rootfs:
            on_rootfs(m_old, template)
        unmount_all()

        # 5. host config that comes from the image: entrypoint + env
        new_cfg = pve.config(temp) or {}
        new_ep, old_ep = new_cfg.get("entrypoint"), cfg.get("entrypoint")
        if new_ep and new_ep != old_ep:
            pve.run(["pct", "set", str(vmid), "--entrypoint", new_ep])
            ui.ok(f"entrypoint → {new_ep}")
        elif not new_ep and old_ep:
            pve.run(["pct", "set", str(vmid), "--delete", "entrypoint"], check=False)
            ui.ok("entrypoint override removed (new image has none)")
        if env_layers is not None:
            # env = new image env + compose layers (replaces the old image's env)
            ctconf.merge_env(vmid, [ctconf.read_env(temp), *env_layers], keep_existing=False)
            ui.ok("env: new image env + compose env written to CT config")

        # 6. cleanup + start
        pve.run(["pct", "destroy", str(temp), "--purge"], check=False)
        temp_created = False
        if was_running:
            pve.run(["pct", "start", str(vmid)])
            ui.ok(f"Refresh complete — CT {vmid} running again")
        else:
            ui.ok(f"Refresh complete — CT {vmid} left stopped as before")
        return template.pull_ref if template else None
    except BaseException as e:
        _recover(pve, vmid, temp, temp_created, destructive, snap, stopped, opts, unmount_all)
        if isinstance(e, ComposeError) or not isinstance(e, Exception):
            raise  # ComposeError, or Ctrl-C / SIGTERM / SIGHUP: keep the original exit status
        raise ComposeError(f"refresh of CT {vmid} failed: {e}") from e


def _abort_or_continue(msg: str, opts: RefreshOptions) -> None:
    if not opts.allow_failed_backup:
        raise ComposeError(msg + " (use --allow-failed-snapshot to continue without a backup)")
    ui.warn(msg)
    ui.warn("Continuing without rollback safety (--allow-failed-snapshot).")


def _backup_or_abort(fn: Callable[[], None], opts: RefreshOptions) -> None:
    try:
        fn()
    except ComposeError as e:
        _abort_or_continue(str(e), opts)


_SHIELD = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)


def _recover(pve, vmid, temp, temp_created, destructive, snap, stopped, opts, unmount_all) -> None:
    # A second Ctrl-C / SIGHUP must not abort the cleanup half-way: ignore them meanwhile.
    saved = {}
    for sig in _SHIELD:
        try:
            saved[sig] = signal.signal(sig, signal.SIG_IGN)
        except ValueError:  # not in the main thread (tests): nothing to shield
            pass
    try:
        _recover_steps(pve, vmid, temp, temp_created, destructive, snap, stopped, opts, unmount_all)
    finally:
        for sig, h in saved.items():
            signal.signal(sig, h)


def _recover_steps(pve, vmid, temp, temp_created, destructive, snap, stopped, opts, unmount_all) -> None:
    ui.err(f"\n=== refresh of CT {vmid} failed — cleaning up ===")
    try:
        unmount_all()
        if temp_created and pve.exists_here(temp):
            if pve.destroy(temp):
                ui.err(f"Removed temp CT {temp}.")
            else:
                ui.err(f"Could not remove temp CT {temp} — run: pct destroy {temp} --purge")
        rolled = False
        if destructive:
            if snap and opts.auto_rollback:
                ui.err(f"Rolling back CT {vmid} to snapshot {snap} …")
                pve.run(["pct", "stop", str(vmid)], check=False)
                if pve.ok(["pct", "rollback", str(vmid), snap]):
                    rolled = True
                    ui.err(f"Rolled back (snapshot kept; remove with: pct delsnapshot {vmid} {snap}).")
                else:
                    ui.err(f"Rollback failed. Recover manually: pct rollback {vmid} {snap}")
            elif snap:
                ui.err(f"Rootfs may be partially replaced. Recover with: pct rollback {vmid} {snap}")
            elif opts.vzdump_storage and opts.pre_backup in ("vzdump", "auto"):
                ui.err(f"Rootfs may be partially replaced. Restore the pre-refresh vzdump from {opts.vzdump_storage}.")
            else:
                ui.err(f"Rootfs may be partially replaced and no pre-refresh backup exists. CT {vmid} left stopped.")
        if stopped and (not destructive or rolled):  # restart only what this run stopped
            if pve.ok(["pct", "start", str(vmid)]):
                ui.err(f"Started CT {vmid} again (previous state).")
    except BaseException as e2:  # noqa: BLE001 - never mask the original error
        ui.err(f"(recovery step failed: {e2})")
