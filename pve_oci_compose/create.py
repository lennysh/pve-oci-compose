"""OCI template pull (``oci-registry-pull`` into vztmpl storage) and ``pct create``."""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field

from . import ctconf, refs, ui
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
        ui.muted(f"Auto-selected template storage '{cands[0]}' (only vztmpl+OCI candidate on '{node}')")
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


def resolve_template(pve: Pve, node: str, ref: str, storage: str | None) -> Template:
    """Read-only: choose storage, normalize + pin the ref, compute the vztmpl volume id."""
    st = pick_template_storage(pve, node, storage)
    norm_in = refs.normalize(ref)
    if norm_in != refs.strip_scheme(ref):
        ui.detail(f"Normalized registry ref: {ref} → {norm_in}")
    pull_ref = norm_in
    if not os.environ.get("OCI_CT_CREATE_NO_RESOLVE_LATEST") and refs.is_floating_latest(
        norm_in, pve.normalize_content_filename(norm_in)
    ):
        pull_ref = refs.resolve_floating(pve, norm_in)
    fname = pve.normalize_content_filename(pull_ref)
    d = pve.vztmpl_dir(st)
    local = os.path.join(d, f"{fname}.tar") if d and os.path.isdir(d) else None
    return Template(st, norm_in, pull_ref, f"{st}:vztmpl/{fname}.tar", local)


def _template_present(pve: Pve, node: str, t: Template) -> bool:
    if t.local_tar and os.path.isfile(t.local_tar):
        return True
    return pve.storage_has_volid(node, t.storage, t.ostemplate)


def pull(pve: Pve, node: str, t: Template, quiet: bool = False, reuse: bool = False) -> None:
    if reuse and _template_present(pve, node, t):
        ui.muted(f"Reusing template {t.ostemplate}")
        return
    if quiet:
        ui.muted(f"Template pull on {t.storage} ({t.pull_ref})")
    else:
        ui.title("Registry pull (vztmpl · oci-registry-pull)")
        ui.kv("Node", node)
        ui.kv("Storage", t.storage)
        if t.input_ref != t.pull_ref:
            ui.kv("Compose ref", t.input_ref)
            ui.kv("Resolved pull ref", t.pull_ref)
        else:
            ui.kv("Image ref", t.pull_ref)
    if pve.dry_run:
        ui.say(f"DRY-RUN: pvesh create /nodes/{node}/storage/{t.storage}/oci-registry-pull --reference {t.pull_ref}")
        return
    if pve.skopeo_inspect(t.pull_ref) is None:
        raise ComposeError(f"registry image not found or not reachable: docker://{t.pull_ref}")
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
        if not quiet:
            ui.kv("Task UPID", upid)
        pve.wait_task(upid, node)
    elif re.search(r"refusing to override|already exists|existing file", out, re.I) and _template_present(pve, node, t):
        ui.muted(f"Template {t.ostemplate} already on storage (delete it there to force a re-download)")
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
    if not quiet:
        ui.ok("Template on storage")


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


def create_ct(pve: Pve, t: Template, spec: CreateSpec, quiet: bool = False,
              pool_autocreated: str | None = None) -> None:
    """pct create + post-create config. Any failure destroys the new CT, then re-raises."""
    if pve.exists_here(spec.vmid):
        raise ComposeError(f"VMID {spec.vmid} already exists")
    argv = ["pct", "create", str(spec.vmid), t.ostemplate, "--rootfs", spec.rootfs, *spec.args]
    if not quiet:
        ui.title("pct create · from vztmpl OCI tarball")
        ui.kv("VMID", spec.vmid)
        ui.kv("Ostemplate", t.ostemplate)
        ui.kv("Rootfs", spec.rootfs)
    if pve.dry_run:
        ui.say(f"DRY-RUN: {ui.redact(argv)}")
        if any(spec.env_layers):
            ui.say(f"DRY-RUN: merge env into /etc/pve/lxc/{spec.vmid}.conf: {sorted({k for l in spec.env_layers for k in l})}")
        if spec.lxc_lines:
            ui.say(f"DRY-RUN: append {len(spec.lxc_lines)} lxc_config_lines to /etc/pve/lxc/{spec.vmid}.conf")
        return
    cp = pve.run(argv, check=False)
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
    except BaseException as e:
        destroy_partial(pve, spec.vmid, pool_autocreated)
        raise ComposeError(f"post-create config of CT {spec.vmid} failed ({e}); new CT removed") from e
    if not quiet:
        ui.ok(f"CT {spec.vmid} created (stopped)")
