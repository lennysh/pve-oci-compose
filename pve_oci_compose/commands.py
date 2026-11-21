"""plan / apply / refresh / pull over the services of a compose file."""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import compose as C
from . import description, inject, marker, pools, refs, ui
from .compose import Compose, Service
from .create import create_ct, destroy_partial, pull, resolve_template, spec_from_service
from .errors import ComposeError
from .guestfs import GuestPathError
from .pve import Pve
from .refresh import RefreshOptions, refresh_ct


@dataclass
class Options:
    service: str | None = None
    vmid: int | None = None
    write_compose: bool = True
    adopt: bool = False
    force: bool = False
    pre_backup: str | None = None
    backup_storage: str | None = None
    allow_failed_snapshot: bool = False


# ---- selection ------------------------------------------------------------------


def select(c: Compose, o: Options) -> list[Service]:
    if not o.service and o.vmid is None:
        return c.services
    out = [
        s for s in c.services
        if (not o.service or s.name == o.service) and (o.vmid is None or s.vmid == o.vmid)
    ]
    if not out:
        if o.service and o.vmid is not None:
            raise ComposeError(f"no service named '{o.service}' with vmid {o.vmid} in {c.path}")
        if o.service:
            raise ComposeError(f"no service named '{o.service}' in {c.path}")
        raise ComposeError(f"no service with vmid {o.vmid} in {c.path} (services with vmid: next cannot match)")
    if len(out) > 1:
        raise ComposeError(f"vmid {o.vmid} matches more than one service in {c.path} (vmids must be unique)")
    what = " / ".join(x for x in (o.service and f"service '{o.service}'", o.vmid is not None and f"vmid {o.vmid}") if x)
    ui.say(f"pve-oci-compose: filter → {what} only")
    return out


# ---- shared helpers ---------------------------------------------------------------


def _inject(pve: Pve, c: Compose, svc: Service, vmid: int, phase: str, root: str) -> None:
    items = svc.inject_items()
    if not items or (phase == "refresh" and not any(inject.on_refresh(i) for i in items)):
        return
    try:
        done = inject.run(
            phase, svc.data, str(c.dir), root, pve.config_lines(vmid) or [], pve.pvesm_path,
            inject.truthy(svc.get("unprivileged"), True),
        )
    except (GuestPathError, OSError, ValueError, KeyError) as e:
        raise ComposeError(f"[{svc.name}] file inject failed: {e}") from e
    for _, g in done:
        ui.say(f"pve-oci-compose: injected {g}")


def _finalize_description(pve: Pve, c: Compose, svc: Service, vmid: int, pulled: str | None) -> None:
    if not description.needs_runtime_update(c, svc):
        return
    text = description.build(c, svc, pulled or svc.image, time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()))
    if text:
        pve.mutate(["pct", "set", str(vmid), "--description", text], check=False)


def _existing_lxc(pve: Pve, vmid: int) -> bool:
    return pve.exists_here(vmid) or pve.vmid_kind(vmid) == "lxc"


def _check_binding(pve: Pve, svc: Service, vmid: int, verb: str) -> marker.Marker | None:
    m = marker.read(pve, vmid)
    if not marker.is_managed(pve, vmid, m):
        raise ComposeError(
            f"{verb}: [{svc.name}] vmid {vmid} is an existing LXC not managed by pve-oci-compose (no guest marker "
            f"{marker.marker_path()} and no pve-oci-compose tag) — e.g. created in the UI. Use a different vmid:, "
            "remove that CT, or align it intentionally with refresh --adopt."
        )
    if m and m.service and m.service != svc.name:
        raise ComposeError(
            f"{verb}: [{svc.name}] vmid {vmid} is bound to compose service '{m.service}', not '{svc.name}'"
        )
    return m


# ---- apply -------------------------------------------------------------------------


def apply(pve: Pve, c: Compose, o: Options) -> None:
    for svc in select(c, o):
        C.validate(svc, c.dir)
        spec_vmid = svc.vmid_spec
        vmid = pve.nextid() if spec_vmid == "next" else spec_vmid
        pool = svc.pool(c.project)

        if _existing_lxc(pve, vmid):
            _check_binding(pve, svc, vmid, "apply")
            ui.say(f"apply: [{svc.name}] vmid {vmid} already exists — skip create")
            if pool:
                pools.ensure_member(pve, pool, vmid)
            continue
        if pve.vmid_kind(vmid) == "qemu":
            raise ComposeError(f"apply: [{svc.name}] vmid {vmid} is used by a QEMU VM (LXC and VM ids share one namespace)")

        ui.say(f"apply: [{svc.name}] creating vmid {vmid} from {svc.image}")
        node = pve.node_name()
        template = resolve_template(pve, node, svc.image, svc.template_storage)
        pull(pve, node, template)
        autocreated = pool if (pool and pools.ensure(pve, pool)) else None
        spec = spec_from_service(svc, vmid, pool, description.build(c, svc))
        create_ct(pve, template, spec, pool_autocreated=autocreated)

        if pve.dry_run:
            ui.say(f"apply: [{svc.name}] DRY-RUN: would tag CT, write guest marker"
                   + (f", inject {len(svc.inject_items())} file(s)" if svc.inject_items() else "")
                   + (", pct start" if spec.start else ""))
            continue

        # Everything up to the marker is all-or-nothing for a new CT.
        try:
            marker.tag(pve, vmid)
            with marker.guest_root(pve, vmid) as root:
                marker.write_at(root, svc.name, svc.image, template.digest)
                _inject(pve, c, svc, vmid, "apply", root)
        except BaseException as e:
            destroy_partial(pve, vmid, autocreated)
            raise ComposeError(f"apply: [{svc.name}] setup of new CT {vmid} failed — CT removed: {e}") from e
        ui.say(f"apply: [{svc.name}] sentinel tag + guest marker ({marker.marker_path()})")

        if spec_vmid == "next":
            if o.write_compose:
                C.write_vmid(c.path, svc.name, vmid)
                ui.say(f"pve-oci-compose: updated vmid for service '{svc.name}' → {vmid} in {c.path}")
            else:
                ui.say(f"apply: [{svc.name}] vmid was 'next' — set vmid: {vmid} in {c.path}")
        _finalize_description(pve, c, svc, vmid, template.pull_ref)
        if pool:
            pools.ensure_member(pve, pool, vmid)
        if spec.start:
            pve.run(["pct", "start", str(vmid)])
            ui.say(f"apply: [{svc.name}] started CT {vmid}")


# ---- drift -------------------------------------------------------------------------


@dataclass
class Drift:
    refresh: bool
    reason: str


def drift(pve: Pve, svc: Service, m: marker.Marker) -> Drift:
    """Should a managed CT be refreshed? Compose ref vs marker ref, then — for the same
    ref — the tag's current registry digest vs the digest recorded at the last pull."""
    if m.ref != svc.image:
        return Drift(True, f"image ref changed: {m.ref} → {svc.image}")
    if not refs.is_registry_ref(svc.image):
        return Drift(False, "image unchanged (local template)")
    if "@sha256:" in svc.image:
        return Drift(False, "image unchanged (pinned digest)")
    if not m.digest:
        return Drift(False, "image ref unchanged; no digest recorded yet (refresh --force once to record it)")
    current = pve.registry_digest(refs.normalize(svc.image))
    if current is None:
        ui.warn(f"[{svc.name}] could not read the registry digest of {svc.image} (offline / rate-limited?)")
        return Drift(False, "image ref unchanged; registry digest unavailable")
    if current != m.digest:
        return Drift(True, f"tag {svc.image} was updated on the registry ({_short(m.digest)} → {_short(current)})")
    return Drift(False, "image unchanged (ref and digest match)")


def _short(d: str) -> str:
    return d.split(":", 1)[-1][:12]


# ---- refresh -------------------------------------------------------------------------


def _refresh_options(svc: Service, o: Options) -> RefreshOptions:
    ro = RefreshOptions()
    ro.pre_backup = o.pre_backup or svc.get("refresh_pre_backup") or "snapshot"
    ro.vzdump_storage = o.backup_storage or svc.get("refresh_backup_storage")
    ro.allow_failed_backup = o.allow_failed_snapshot
    ro.vzdump_remove = inject.truthy(svc.get("refresh_vzdump_remove"))
    ro.vzdump_protected = inject.truthy(svc.get("refresh_vzdump_protected"), True)
    ro.template_storage = svc.template_storage
    return ro


def refresh(pve: Pve, c: Compose, o: Options) -> None:
    for svc in select(c, o):
        if svc.vmid is None:
            raise ComposeError(f"service {svc.name}: refresh needs a numeric vmid (still 'next' — run apply first)")
        C.validate(svc, c.dir)
        vmid = svc.vmid
        if not pve.exists_here(vmid):
            node = pve.lxc_node(vmid)
            if node and node != pve.node_name():
                raise ComposeError(
                    f"refresh: [{svc.name}] CT {vmid} runs on node '{node}', this is '{pve.node_name()}'. "
                    f"refresh needs pct mount/rsync on the owning host — run it on '{node}'."
                )
            raise ComposeError(f"refresh: [{svc.name}] vmid {vmid} does not exist (or pct config failed here)")

        m = marker.read(pve, vmid)
        if not (m and m.ref):
            if not o.adopt:
                ui.say(f"refresh: [{svc.name}] CT {vmid} has no readable guest marker — skip (use --adopt for unmanaged CTs)")
                continue
            ui.say(f"refresh: [{svc.name}] --adopt: treating unmanaged CT {vmid} as {svc.name}")
        elif m.service and m.service != svc.name and not o.adopt:
            raise ComposeError(f"refresh: [{svc.name}] CT {vmid} is bound to service '{m.service}' (use --adopt to rebind)")
        elif not o.force:
            d = drift(pve, svc, m)
            if not d.refresh:
                ui.say(f"refresh: [{svc.name}] {d.reason} — skip")
                continue
            ui.say(f"refresh: [{svc.name}] {d.reason}")

        ui.say(f"refresh: [{svc.name}] vmid {vmid} → {svc.image}")

        def on_rootfs(root: str, template, svc=svc, vmid=vmid) -> None:
            marker.write_at(root, svc.name, svc.image, template.digest if template else None)
            _inject(pve, c, svc, vmid, "refresh", root)

        pulled = refresh_ct(pve, vmid, svc.image, _refresh_options(svc, o), on_rootfs,
                            env_layers=[svc.defaults_env, svc.service_env])
        if pve.dry_run:
            continue
        marker.tag(pve, vmid)
        _finalize_description(pve, c, svc, vmid, pulled)
        pool = svc.pool(c.project)
        if pool:
            pools.ensure_member(pve, pool, vmid)


# ---- pull ------------------------------------------------------------------------------


def pull_only(pve: Pve, c: Compose, o: Options) -> None:
    for svc in select(c, o):
        C.validate(svc, c.dir, need_rootfs=False)
        ui.say(f"pull: [{svc.name}] {svc.image}")
        node = pve.node_name()
        t = resolve_template(pve, node, svc.image, svc.template_storage)
        pull(pve, node, t)


# ---- plan --------------------------------------------------------------------------------


def plan(pve: Pve, c: Compose, o: Options) -> None:
    ui.say(f"Compose file: {c.path}")
    if c.project:
        ui.say(f"Project: {c.project}")
    services = select(c, o)
    ui.say()
    for svc in services:
        C.validate(svc, c.dir)
        vmid = svc.vmid
        head = f"=== service: {svc.name} (vmid {vmid if vmid is not None else 'next'}) ==="
        if vmid is None:
            try:
                nxt = str(pve.nextid())
            except ComposeError:
                nxt = "(query failed — need pvesh on a PVE node)"
            ui.say(f"=== service: {svc.name} (vmid next (next free now: {nxt})) ===")
        else:
            ui.say(head)
        ui.say(f"  image (file):  {svc.image}")
        ui.say(f"  rootfs:        {svc.get('rootfs')}")
        for key in ("mounts", "bind_mounts"):
            if svc.strings(key):
                ui.say(f"  {key + ':':<14} {' | '.join(svc.strings(key))}")
        if svc.strings("lxc_config_lines"):
            ui.say(f"  lxc_config_lines: {len(svc.strings('lxc_config_lines'))} line(s) (new CT only)")
        items = svc.inject_items()
        if items:
            nr = sum(1 for i in items if inject.on_refresh(i))
            ui.say(f"  file inject:   {len(items)} file(s) (apply: all; refresh: {nr} with on_refresh)")
        if svc.env:
            ui.say(f"  env (compose): {', '.join(sorted(svc.env))}")
        pool = svc.pool(c.project)
        ui.say(f"  pool (target): {pool or '<none>'}")
        desc = description.build(c, svc)
        if desc:
            ui.say("  pct description (preview):")
            for line in desc.splitlines():
                ui.say(f"    {line}")

        if vmid is None:
            ui.say(f"  plan apply:    would allocate next free vmid and create from {svc.image}")
            ui.say("  plan refresh:  n/a until vmid is fixed in the file (apply writes it)")
            ui.say()
            continue

        local = pve.exists_here(vmid)
        kind = pve.vmid_kind(vmid)
        exists = local or kind == "lxc"
        ui.say(f"  exists:        {'yes' if exists else 'no'}")
        if not exists:
            if kind == "qemu":
                ui.say(f"  WARNING:       vmid {vmid} is used by a QEMU VM; apply would refuse.")
            ui.say(f"  plan apply:    would create from {svc.image}")
            ui.say("  plan refresh:  n/a (CT missing)")
            ui.say()
            continue
        if not local:
            node = pve.lxc_node(vmid)
            ui.say(f"  cluster note:  CT runs on node '{node or '?'}'; plan details need that node.")
        tags = pve.tags(vmid)
        m = marker.read(pve, vmid) if local else None
        ui.say(f"  tags:          {tags or '<none>'}")
        if not marker.is_managed(pve, vmid, m):
            ui.say(f"  WARNING:       vmid {vmid} exists but is not compose-managed (no marker, no pve-oci-compose tag).")
            ui.say("  plan apply:    would REFUSE (cannot create over that CT)")
            ui.say("  plan refresh:  blocked (use --adopt only after aligning this CT with the service)")
        elif m and m.service and m.service != svc.name:
            ui.say(f"  WARNING:       guest marker service is '{m.service}', compose key is '{svc.name}'.")
            ui.say("  plan apply:    would REFUSE (wrong service name for this vmid)")
        else:
            ui.say(f"  stored ref:    {m.ref if m and m.ref else '<unreadable here>'}")
            if m and m.digest:
                ui.say(f"  stored digest: {m.digest}")
            if not (m and m.ref):
                ui.say("  plan refresh:  drift check needs a readable guest marker")
            else:
                d = drift(pve, svc, m)
                ui.say(f"  plan refresh:  {'would run refresh — ' if d.refresh else 'skip — '}{d.reason}")
            ui.say("  plan apply:    no-op (already exists, compose-managed)")
        ui.say()
