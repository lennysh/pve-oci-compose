"""apply / refresh end-to-end against FakePve."""

import json
import os

import pytest

from fakepve import FakePve
from pve_oci_compose import commands, ctconf
from pve_oci_compose import compose as C
from pve_oci_compose.errors import ComposeError

COMPOSE = """\
name: stack
services:
  web:
    vmid: next
    image: ghcr.io/o/web:1.0
    rootfs: local-zfs:8
    start: true
    env: {APP: "1"}
    rootfs_files:
      - path: /etc/app.conf
        content: "hello\\n"
"""


@pytest.fixture
def setup(tmp_path):
    p = tmp_path / "compose.yaml"
    p.write_text(COMPOSE)
    pve = FakePve(tmp_path)
    return pve, p


def test_apply_creates_tags_marks_writes_back_and_starts(setup, tmp_path):
    pve, p = setup
    commands.apply(pve, C.load(p), commands.Options())
    assert 200 in pve.cts
    root = tmp_path / "lxc/200/rootfs"
    assert json.loads((root / "etc/pve-oci-compose.json").read_text()) == {
        "service": "web", "ref": "ghcr.io/o/web:1.0", "digest": "sha256:" + "0" * 64}
    assert (root / "etc/app.conf").read_text() == "hello\n"
    conf = (ctconf.CONF_DIR / "200.conf").read_text()
    assert "tags: pve-oci-compose" in conf
    assert "env: PATH=/usr/bin\0IMAGE_VAR=1\0APP=1" in conf
    assert "vmid: 200" in p.read_text()
    assert pve.cts[200]["status"] == "running"
    create = pve.cmds("pct create")[0]
    assert "--start" not in create and "--pool" in create and pve.pools["stack"] == [200]
    # second apply is a no-op skip
    pve.calls.clear()
    commands.apply(pve, C.load(p), commands.Options())
    assert not pve.cmds("pct create")


def test_apply_failure_after_create_removes_ct_and_keeps_compose(setup, tmp_path, monkeypatch):
    pve, p = setup
    # plant a symlink that the marker write must refuse → setup failure after pct create
    real_add = pve.add_ct

    def add_with_trap(vmid, conf, status="stopped"):
        root = real_add(vmid, conf, status)
        os.symlink("/etc/passwd", root / "etc/pve-oci-compose.json")
        return root

    monkeypatch.setattr(pve, "add_ct", add_with_trap)
    with pytest.raises(ComposeError, match="CT removed"):
        commands.apply(pve, C.load(p), commands.Options())
    assert 200 not in pve.cts
    assert "vmid: next" in p.read_text()
    assert "stack" not in pve.pools or pve.cmds("pvesh delete")  # auto-created pool removed


def test_apply_refuses_unmanaged_existing_ct(setup):
    pve, p = setup
    pve.add_ct(200, ["rootfs: local-zfs:subvol-200-disk-0,size=8G"])
    p.write_text(COMPOSE.replace("vmid: next", "vmid: 200"))
    with pytest.raises(ComposeError, match="not managed"):
        commands.apply(pve, C.load(p), commands.Options())
    assert not pve.cmds("pct create")


def test_apply_refuses_qemu_vmid(setup):
    pve, p = setup
    pve.qemu.add(200)
    pve.next_id = 200
    with pytest.raises(ComposeError, match="QEMU"):
        commands.apply(pve, C.load(p), commands.Options())


def test_dry_run_changes_nothing(tmp_path):
    p = tmp_path / "compose.yaml"
    p.write_text(COMPOSE)
    pve = FakePve(tmp_path, dry_run=True)
    commands.apply(pve, C.load(p), commands.Options())
    assert not pve.cts and "vmid: next" in p.read_text()


def _managed(pve, tmp_path, vmid=105, ref="ghcr.io/o/web:0.9", status="running"):
    root = pve.add_ct(vmid, [
        "hostname: web", "rootfs: local-zfs:subvol-105-disk-0,size=8G", "tags: pve-oci-compose",
        "mp0: /mnt/nfs,mp=/data", "env: PATH=/usr/bin\0OLD_IMAGE_VAR=x", "unprivileged: 1",
    ], status=status)
    (root / "etc/pve-oci-compose.json").write_text(json.dumps({"service": "web", "ref": ref}))
    return root


def _refresh_compose(tmp_path, image="ghcr.io/o/web:1.0"):
    p = tmp_path / "compose.yaml"
    p.write_text(f"services:\n  web:\n    vmid: 105\n    image: {image}\n    rootfs: local-zfs:8\n"
                 "    env: {APP: '2'}\n")
    return C.load(p)


def test_refresh_success(tmp_path):
    pve = FakePve(tmp_path)
    root = _managed(pve, tmp_path)
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    rsync = pve.cmds("rsync")[0]
    assert "--exclude=/data" in rsync and "--delete" in rsync
    assert json.loads((root / "etc/pve-oci-compose.json").read_text())["ref"] == "ghcr.io/o/web:1.0"
    conf = (ctconf.CONF_DIR / "105.conf").read_text()
    assert "env: PATH=/usr/bin\0IMAGE_VAR=1\0APP=2" in conf and "OLD_IMAGE_VAR" not in conf
    assert pve.cts[105]["status"] == "running"
    assert 200 not in pve.cts  # temp destroyed
    assert pve.cmds("pct snapshot 105")


def test_refresh_skips_when_unchanged_and_unmanaged(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path, ref="ghcr.io/o/web:1.0")
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert not pve.cmds("rsync")
    (tmp_path / "lxc/105/rootfs/etc/pve-oci-compose.json").unlink()
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert not pve.cmds("rsync")


def test_refresh_rsync_failure_rolls_back_and_restarts(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    pve.fail["rsync"] = 23
    with pytest.raises(ComposeError):
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    snap = pve.cmds("pct snapshot 105")[0][3]
    assert pve.cmds(f"pct rollback 105 {snap}")
    assert pve.cts[105]["status"] == "running"
    assert 200 not in pve.cts


def test_refresh_failure_before_rsync_does_not_roll_back(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    pve.fail["pct create"] = 1
    with pytest.raises(ComposeError):
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert not pve.cmds("pct rollback") and not pve.cmds("rsync")
    assert pve.cts[105]["status"] == "running"


def test_refresh_snapshot_failure_aborts_before_touching(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path, status="stopped")
    pve.fail["pct snapshot"] = 2
    with pytest.raises(ComposeError, match="snapshot failed"):
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert not pve.cmds("pct create") and pve.cts[105]["status"] == "stopped"


def test_select_filters(tmp_path):
    c = _refresh_compose(tmp_path)
    assert [s.name for s in commands.select(c, commands.Options(vmid=105))] == ["web"]
    with pytest.raises(ComposeError):
        commands.select(c, commands.Options(service="nope"))


# ---- digest drift ---------------------------------------------------------------------

D_OLD, D_NEW = "sha256:" + "a" * 64, "sha256:" + "b" * 64


def _same_ref(pve, tmp_path, digest=D_OLD, registry=D_NEW, image="ghcr.io/o/web:1.0"):
    root = _managed(pve, tmp_path, ref=image)
    blob = {"service": "web", "ref": image}
    if digest:
        blob["digest"] = digest
    (root / "etc/pve-oci-compose.json").write_text(json.dumps(blob))
    pve.digests[image] = registry
    return root, _refresh_compose(tmp_path, image)


def test_refresh_when_tag_moved_on_registry(tmp_path):
    pve = FakePve(tmp_path)
    root, c = _same_ref(pve, tmp_path)
    commands.refresh(pve, c, commands.Options())
    assert pve.cmds("rsync")
    assert json.loads((root / "etc/pve-oci-compose.json").read_text())["digest"] == D_NEW


def test_skip_when_digest_matches(tmp_path, capsys):
    pve = FakePve(tmp_path)
    _, c = _same_ref(pve, tmp_path, registry=D_OLD)
    commands.refresh(pve, c, commands.Options())
    assert not pve.cmds("rsync")
    assert "ref and digest match" in capsys.readouterr().out


def test_old_marker_without_digest_skips_with_hint(tmp_path, capsys):
    pve = FakePve(tmp_path)
    root, c = _same_ref(pve, tmp_path, digest=None)
    commands.refresh(pve, c, commands.Options())
    assert not pve.cmds("rsync")
    assert "--force once" in capsys.readouterr().out
    commands.refresh(pve, c, commands.Options(force=True))  # records the digest
    assert json.loads((root / "etc/pve-oci-compose.json").read_text())["digest"] == D_NEW


def test_registry_unreachable_skips(tmp_path):
    pve = FakePve(tmp_path)
    _, c = _same_ref(pve, tmp_path, registry=None)
    commands.refresh(pve, c, commands.Options())
    assert not pve.cmds("rsync")


def test_pinned_digest_never_checks_registry(tmp_path, monkeypatch):
    pve = FakePve(tmp_path)
    img = "ghcr.io/o/web@" + D_OLD
    _same_ref(pve, tmp_path, image=img)
    monkeypatch.setattr(pve, "registry_digest", lambda r: pytest.fail("registry queried"))
    commands.refresh(pve, _refresh_compose(tmp_path, img), commands.Options())
    assert not pve.cmds("rsync")


def test_plan_reports_digest_drift(tmp_path, capsys):
    pve = FakePve(tmp_path)
    _, c = _same_ref(pve, tmp_path)
    commands.plan(pve, c, commands.Options())
    out = capsys.readouterr().out
    assert f"stored digest: {D_OLD}" in out
    assert "would run refresh — tag ghcr.io/o/web:1.0 was updated on the registry" in out


# ---- template reuse -----------------------------------------------------------------------


def test_template_reused_only_when_digest_current(tmp_path, setup):
    from pve_oci_compose import state
    from pve_oci_compose.create import pull, resolve_template

    pve, _ = setup
    pve.digests["ghcr.io/o/web:1.0"] = D_OLD
    t = resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None)
    pull(pve, "pve1", t)
    assert state.template_digest(t.ostemplate) == D_OLD and len(pve.cmds("pvesh create")) == 1

    pull(pve, "pve1", t)  # same digest: reuse, no download
    assert len(pve.cmds("pvesh create")) == 1 and not pve.cmds("pvesm free")

    pve.digests["ghcr.io/o/web:1.0"] = D_NEW  # tag moved: stale tarball removed + re-pulled
    t2 = resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None)
    pull(pve, "pve1", t2)
    assert pve.cmds(f"pvesm free {t.ostemplate}") and len(pve.cmds("pvesh create")) == 2
    assert state.template_digest(t.ostemplate) == D_NEW


def test_unknown_template_on_storage_is_replaced(tmp_path, setup):
    from pve_oci_compose.create import pull, resolve_template

    pve, _ = setup
    t = resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None)
    pve.volids.add(t.ostemplate)  # pulled earlier by something else: digest unknown
    pull(pve, "pve1", t)
    assert pve.cmds(f"pvesm free {t.ostemplate}") and pve.cmds("pvesh create")
