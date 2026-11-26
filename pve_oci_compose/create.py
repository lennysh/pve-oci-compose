"""OCI template pull (``oci-registry-pull`` into vztmpl storage) and ``pct create``."""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from dataclasses import dataclass, field

from . import ctconf, refs, state, ui
from .compose import Service, as_flag, as_scalar, parse_mount_spec
from .errors import CommandError, ComposeError
from .pve import OCI_PULL_STORAGE_TYPES, Pve, parse_upid

DEFAULT_NET0 = "name=eth0,bridge=vmbr0,ip=dhcp"


# ---- template storage ------------------------------------------------------------


def oci_storages(storages: list[dict]) -> list[str]:
    ids = {
        str(s.get("storage"))
        for s in storages
        if "vztmpl" in str(s.get("content", "")) and s.get("type") in OCI_PULL_STORAGE_TYPES
    }
    return sorted(i for i in ids if i and i != "None")


def pick_template_storage(pve: Pve, node: str, requested: str | None) -> str:
    stores = pve.storages(node)
    cands = oci_storages(stores)
    listing = ", ".join(cands) or "none"
    if requested:
        if requested.strip() not in cands:
            raise ComposeError(
                f"template storage '{requested}' cannot hold OCI pulls on node '{node}' (needs vztmpl content "
                f"and type dir/nfs/cifs). Usable here: {listing}"
            )
        return requested.strip()
    if len(cands) == 1:
        ui.detail(f"template storage {cands[0]} (only OCI-capable store on {node})")
        return cands[0]
    if not cands:
        raise ComposeError(
            f"no storage on node '{node}' can hold OCI pulls (enable 'Container template' on a dir/nfs/cifs store)"
        )
    raise ComposeError(f"several template storages on '{node}' ({listing}) — set template_storage: on the service")


# ---- pull --------------------------------------------------------------------------


@dataclass
class Template:
    storage: str
    input_ref: str
    pull_ref: str
    ostemplate: str
    local_tar: str | None
    digest: str | None = None  # registry digest of input_ref at resolve time (before the pull)


def resolve_template(pve: Pve, node: str, ref: str, storage: str | None) -> Template:
    """Read-only: choose storage, normalize + pin the ref, compute the vztmpl volume id."""
    st = pick_template_storage(pve, node, storage)
    norm_in = refs.normalize(ref)
    if norm_in != refs.strip_scheme(ref):
        ui.detail(f"Normalized registry ref: {ref} → {norm_in}")
    # Fetched before the pull: if the tag moves in between, the recorded digest is the older
    # one and the next refresh re-checks (never the other way round).
    digest = pve.registry_digest(norm_in)
    pull_ref = norm_in
    if not os.environ.get("OCI_CT_CREATE_NO_RESOLVE_LATEST") and refs.is_floating_latest(
        norm_in, pve.normalize_content_filename(norm_in)
    ):
        pull_ref = refs.resolve_floating(pve, norm_in, digest)
    fname = pve.normalize_content_filename(pull_ref)
    d = pve.vztmpl_dir(st)
    local = os.path.join(d, f"{fname}.tar") if d and os.path.isdir(d) else None
    return Template(st, norm_in, pull_ref, f"{st}:vztmpl/{fname}.tar", local, digest)


def _template_present(pve: Pve, node: str, t: Template) -> bool:
    if t.local_tar and os.path.isfile(t.local_tar):
        return True
    return pve.storage_has_volid(node, t.storage, t.ostemplate)


def template_is_current(pve: Pve, node: str, t: Template) -> bool:
    """Template on storage and pulled at the digest the tag has now (digest refs: immutable)."""
    if not _template_present(pve, node, t):
        return False
    if "@sha256:" in t.pull_ref:
        return True
    return bool(t.digest) and state.template_digest(t.ostemplate) == t.digest


def pull(pve: Pve, node: str, t: Template) -> None:
    """Make sure ``t.ostemplate`` holds the image the tag points at *now*.

    Reuses the template when its recorded digest matches; otherwise a template with the
    same filename (older pull of a moved tag, or unknown origin) is removed and re-pulled.
    """
    if template_is_current(pve, node, t):
        ui.muted(f"template already current on {t.storage} — reused")
        return
    if t.input_ref != t.pull_ref:
        ui.muted(f"pinned {t.input_ref.rsplit('/', 1)[-1]} → {t.pull_ref.rsplit('/', 1)[-1]}")
    present = _template_present(pve, node, t)
    if pve.dry_run:
        if present:
            ui.say(f"DRY-RUN: pvesm free {t.ostemplate}   # stale or unknown digest")
        ui.say(f"DRY-RUN: pvesh create /nodes/{node}/storage/{t.storage}/oci-registry-pull --reference {t.pull_ref}")
        return
    if not t.digest:
        raise ComposeError(f"registry image not found or not reachable: docker://{t.input_ref}")
    if present:
        ui.muted("replaced outdated template (tag moved or digest unknown)")
        pve.run(["pvesm", "free", t.ostemplate])
        state.record_template(t.ostemplate, None)
    cp = pve.run(
        ["pvesh", "create", f"/nodes/{node}/storage/{t.storage}/oci-registry-pull",
         "--reference", t.pull_ref, "--output-format", "json"],
        check=False,
    )
    out = cp.stdout or ""
    if cp.returncode == 0:
        upid = parse_upid(out)
        if not upid:
            raise ComposeError(f"could not parse task UPID from oci-registry-pull: {out[:300]}")
        ui.detail(f"task {upid}")
        pve.wait_task(upid, node)
    elif re.search(r"refusing to override|already exists|existing file", out, re.I):
        raise ComposeError(
            f"template {t.ostemplate} reappeared during the pull (concurrent run?) — re-run, or remove it with "
            f"pvesm free {t.ostemplate}"
        )
    elif "@sha256:" in t.pull_ref and t.local_tar and re.search(
        r"parameter verification|invalid|does not match|malformed|bad request", out, re.I
    ):
        ui.muted("oci-registry-pull rejected the digest ref; copying with skopeo")
        tmp = f"{t.local_tar}.pulltmp.{os.getpid()}"
        try:
            pve.run(["skopeo", "copy", f"docker://{t.pull_ref}", f"oci-archive:{tmp}"], stream=True)
            os.replace(tmp, t.local_tar)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    else:
        raise ComposeError(f"oci-registry-pull failed on storage '{t.storage}': {out.strip()[-600:]}")
    for _ in range(20):
        if _template_present(pve, node, t):
            break
        time.sleep(1)
    else:
        raise ComposeError(f"template {t.ostemplate} did not appear on storage after pull")
    state.record_template(t.ostemplate, t.digest)


# ---- pct create -----------------------------------------------------------------


@dataclass
class CreateSpec:
    vmid: int
    rootfs: str
    args: list[str] = field(default_factory=list)  # pct flags after the ostemplate
    env_layers: list[dict[str, str]] = field(default_factory=list)
    lxc_lines: list[str] = field(default_factory=list)
    pool: str | None = None
    start: bool = False
    privileged: bool = False  # built unprivileged, then converted (see make_privileged)


_SCALAR_FLAGS = [
    ("hostname", "--hostname"), ("memory", "--memory"), ("swap", "--swap"), ("cores", "--cores"),
    ("cpulimit", "--cpulimit"), ("cpuunits", "--cpuunits"), ("ostype", "--ostype"), ("arch", "--arch"),
    ("features", "--features"), ("entrypoint", "--entrypoint"), ("tags", "--tags"),
    ("timezone", "--timezone"), ("password", "--password"), ("ssh_public_keys", "--ssh-public-keys"),
    ("startup", "--startup"), ("hookscript", "--hookscript"), ("cmode", "--cmode"),
    ("console", "--console"), ("tty", "--tty"), ("bwlimit", "--bwlimit"),
]
_BOOL_FLAGS = [
    ("protection", "--protection"), ("ha_managed", "--ha-managed"),
    ("ignore_unpack_errors", "--ignore-unpack-errors"), ("pct_debug", "--debug"),
]


def spec_from_service(svc: Service, vmid: int, pool: str | None, description: str | None) -> CreateSpec:
    a: list[str] = []
    nets = sorted((k for k in svc.data if re.fullmatch(r"net\d+", k)), key=lambda k: int(k[3:]))
    if not nets:
        a += ["--net0", os.environ.get("OCI_CT_CREATE_NET0") or DEFAULT_NET0]
    for k in nets:
        a += [f"--{k}", str(svc.data[k])]
    a += ["--unprivileged", as_flag(svc.get("unprivileged")) or "1"]
    a += ["--onboot", as_flag(svc.get("onboot")) or "0"]
    for key, flag in _SCALAR_FLAGS:
        v = as_scalar(svc.get(key))
        if v:
            a += [flag, v]
    if "hostname" not in svc.data:
        a += ["--hostname", f"oci-ct-{vmid}"]
    for key, flag in _BOOL_FLAGS:
        v = as_flag(svc.get(key))
        if v is not None:
            a += [flag, v]
    ns = svc.get("nameserver")
    if isinstance(ns, list):
        servers = [str(x) for x in ns]
    elif ns is not None:
        servers = [x for x in re.split(r"[,\s]+", str(ns)) if x]
    else:
        servers = []
    if servers:
        a += ["--nameserver", " ".join(servers)]
    sd = svc.get("searchdomain")
    if sd is not None:
        a += ["--searchdomain", " ".join(map(str, sd)) if isinstance(sd, list) else str(sd)]
    if description:
        a += ["--description", description]
    if pool:
        a += ["--pool", pool]
    idx = 0
    for m in svc.strings("mounts"):
        a += [f"--mp{idx}", parse_mount_spec(m, svc.name)]
        idx += 1
    for b in svc.strings("bind_mounts"):
        a += [f"--mp{idx}", b]
        idx += 1
    for i, d in enumerate(svc.strings("lxc_dev")):
        a += [f"--dev{i}", d]
    for i, u in enumerate(svc.strings("unused_disks")):
        a += [f"--unused{i}", u]
    return CreateSpec(
        vmid=vmid,
        rootfs=str(svc.get("rootfs")),
        args=a,
        env_layers=[svc.defaults_env, svc.service_env],
        lxc_lines=svc.strings("lxc_config_lines"),
        pool=pool,
        start=as_flag(svc.get("start")) == "1",
        privileged=as_flag(svc.get("unprivileged")) == "0",
    )


def destroy_partial(pve: Pve, vmid: int, pool_autocreated: str | None = None) -> None:
    """Remove a CT left behind by a failed create (and a pool this run auto-created)."""
    if pve.exists_here(vmid):
        ui.err(f"pve-oci-compose: removing partial CT {vmid} …")
        if not pve.destroy(vmid):
            ui.err(f"pve-oci-compose: could not remove CT {vmid} (protection: 1?) — remove it manually")
    if pool_autocreated:
        if pve.ok(["pvesh", "delete", f"/pools/{pool_autocreated}"]) or pve.ok(
            ["pvesh", "delete", "/pools", "--poolid", pool_autocreated]
        ):
            ui.err(f"pve-oci-compose: removed auto-created pool '{pool_autocreated}'")


# Options that must not be set while the CT is still the unprivileged intermediate:
# a protected CT cannot be overwritten by the restore, and HA would race it.
_DEFER_FOR_RESTORE = ("--protection", "--ha-managed")
PRIVILEGED_NOTE = "privileged: built unprivileged, then converted via vzdump → pct restore --unprivileged 0"


def _pairs(args: list[str]) -> list[tuple[str, str]]:
    return list(zip(args[::2], args[1::2]))


def privileged_plan(spec: CreateSpec) -> tuple[list[str], list[str], list[str]]:
    """(create args, restore volume args, restore extra args) for a privileged CT.

    Proxmox (pve-container ≤ 6.1.x) cannot unpack an OCI image into a privileged CT: the
    layers are always extracted in a user namespace, and a privileged CT has no id map
    (``setgid(0): Invalid argument``). So the CT is created unprivileged and converted.

    The restore gets **only** the volume specs (+ deferred flags): everything else stays
    as Proxmox derived it from the image at create time. Re-passing e.g. ``--net0`` would
    drop the ``host-managed=1`` that OCI create enabled, and change the MAC address.
    """
    create: list[str] = []
    extra: list[str] = []
    volumes = ["--rootfs", spec.rootfs]
    for flag, val in _pairs(spec.args):
        if flag == "--unprivileged":
            create += [flag, "1"]
        elif flag in _DEFER_FOR_RESTORE:
            extra += [flag, val]
        else:
            create += [flag, val]
            if re.fullmatch(r"--mp\d+", flag):
                volumes += [flag, val]
    return create, volumes, extra


def make_privileged(pve: Pve, vmid: int, volume_args: list[str], extra_args: list[str]) -> None:
    """Convert a freshly created unprivileged CT to privileged, keeping its config.

    vzdump archives the rootfs from inside the CT's id mapping (so the archive holds plain
    0–65535 ids); restoring it with --unprivileged 0 extracts without a user namespace.
    Mount-point volumes are recreated and their contents carried over; bind mounts kept.
    """
    base = Path(os.environ.get("PVE_OCI_PRIV_DUMPDIR") or "/var/tmp/pve-oci-compose")
    d = base / f"priv-{vmid}-{os.getpid()}"
    d.mkdir(parents=True, exist_ok=True)
    try:
        pve.run(["vzdump", str(vmid), "--dumpdir", str(d), "--mode", "stop", "--compress", "zstd"])
        archives = sorted(d.glob(f"vzdump-lxc-{vmid}-*.tar.zst"))
        if not archives:
            raise ComposeError(f"vzdump of CT {vmid} produced no archive in {d}")
        pve.run(["pct", "restore", str(vmid), str(archives[-1]), "--force", "1", "--unprivileged", "0",
                 *volume_args, *extra_args])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# pve-container ≤ 6.1.x: privileged OCI create dies while unpacking (no id map for the
# user namespace). Matched to fall back to make_privileged(); once Proxmox ships the fix the
# direct create simply succeeds and no conversion happens.
_PRIV_OCI_BUG = re.compile(r"setgid\(0\): Invalid argument")


def _run_create(pve: Pve, argv: list[str], vmid: int, pool_autocreated: str | None):
    try:
        cp = pve.run(argv, check=False)
    except BaseException:  # Ctrl-C / SIGTERM / SIGHUP while pct create runs
        destroy_partial(pve, vmid, pool_autocreated)
        raise
    if cp.returncode != 0:
        destroy_partial(pve, vmid, None)
    return cp


def create_ct(pve: Pve, t: Template, spec: CreateSpec,
              pool_autocreated: str | None = None) -> None:
    """pct create + post-create config. Any failure destroys the new CT, then re-raises.

    Privileged CTs are created directly when Proxmox can; on the known OCI unpack bug they
    are created unprivileged and converted (see privileged_plan / make_privileged)."""
    if pve.exists_here(spec.vmid):
        raise ComposeError(f"VMID {spec.vmid} already exists")
    argv = ["pct", "create", str(spec.vmid), t.ostemplate, "--rootfs", spec.rootfs, *spec.args]
    if pve.dry_run:
        ui.say(f"DRY-RUN: {ui.redact(argv)}")
        if spec.privileged:
            _, volumes, deferred = privileged_plan(spec)
            ui.say("DRY-RUN: if Proxmox rejects the privileged OCI create (setgid(0) bug): create with "
                   f"--unprivileged 1, vzdump {spec.vmid} --dumpdir …, pct restore {spec.vmid} <archive> "
                   f"--force 1 --unprivileged 0 {' '.join(volumes + deferred)}")
        if any(spec.env_layers):
            ui.say(f"DRY-RUN: merge env into /etc/pve/lxc/{spec.vmid}.conf: {sorted({k for l in spec.env_layers for k in l})}")
        if spec.lxc_lines:
            ui.say(f"DRY-RUN: append {len(spec.lxc_lines)} lxc_config_lines to /etc/pve/lxc/{spec.vmid}.conf")
        return

    convert: tuple[list[str], list[str]] | None = None
    cp = _run_create(pve, argv, spec.vmid, pool_autocreated)
    if cp.returncode != 0 and spec.privileged and _PRIV_OCI_BUG.search(cp.stdout or ""):
        create_args, volumes, deferred = privileged_plan(spec)
        convert = (volumes, deferred)
        argv = ["pct", "create", str(spec.vmid), t.ostemplate, "--rootfs", spec.rootfs, *create_args]
        cp = _run_create(pve, argv, spec.vmid, pool_autocreated)
    if cp.returncode != 0:
        ui.err(cp.stdout or "")
        destroy_partial(pve, spec.vmid, pool_autocreated)
        if re.search(r"Wrong media type|Error while parsing OCI image", cp.stdout or ""):
            raise ComposeError("Proxmox rejected this OCI archive (unsupported manifest/layer media types)")
        raise CommandError(argv[:3], cp.returncode)
    try:
        if any(spec.env_layers):
            ctconf.merge_env(spec.vmid, spec.env_layers)
        ctconf.append_lines(spec.vmid, spec.lxc_lines)
        if convert:
            make_privileged(pve, spec.vmid, *convert)
            ui.muted(PRIVILEGED_NOTE)
    except BaseException as e:
        destroy_partial(pve, spec.vmid, pool_autocreated)
        raise ComposeError(f"post-create setup of CT {spec.vmid} failed ({e}); new CT removed") from e
