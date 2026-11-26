"""apply / refresh end-to-end against FakePve."""

import json
import os
from pathlib import Path

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
        "service": "web", "ref": "ghcr.io/o/web:1.0", "digest": "sha256:" + "0" * 64, "version": "1.0",
        "platform": "linux/amd64"}
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
    # the bad image failed while the target was still running: no downtime at all
    assert not pve.cmds("pct shutdown") and not pve.cmds("pct snapshot") and not pve.cmds("pct start")
    assert pve.cts[105]["status"] == "running"


def test_refresh_order_minimises_downtime(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())

    def first(prefix):
        return next(i for i, c in enumerate(pve.calls) if " ".join(c).startswith(prefix))

    assert (first("skopeo copy") < first("pct create 200") < first("pct shutdown 105")
            < first("pct snapshot 105") < first("rsync") < first("pct start 105"))


def test_refresh_snapshot_failure_aborts_before_touching(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path, status="stopped")
    pve.fail["pct snapshot"] = 2
    with pytest.raises(ComposeError, match="snapshot failed"):
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert not pve.cmds("rsync") and pve.cts[105]["status"] == "stopped"
    assert 200 not in pve.cts  # temp CT (created first) removed again


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
    monkeypatch.setattr(pve, "registry_resolve", lambda r: pytest.fail("registry queried"))
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


def test_pull_is_by_exact_digest_into_digest_named_template(tmp_path, setup):
    from pve_oci_compose.create import pull, resolve_template

    pve, _ = setup
    pve.digests["ghcr.io/o/web:1.0"] = D_OLD
    t = resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None)
    assert t.ostemplate == f"local:vztmpl/ghcr.io_o_web_1.0_sha256-{'a' * 16}.tar"
    pull(pve, "pve1", t)
    copy = pve.cmds("skopeo copy")[0]
    assert f"docker://ghcr.io/o/web@{D_OLD}" in copy  # never re-resolves the tag
    pull(pve, "pve1", t)  # same digest: file exists → reused
    assert len(pve.cmds("skopeo copy")) == 1


def test_moved_tag_pulls_new_file_and_prunes_only_own_old_templates(tmp_path, setup):
    from pve_oci_compose.create import pull, resolve_template

    pve, _ = setup
    tdir = Path(pve.vztmpl_dir("local"))
    (tdir / "ghcr.io_o_web_1.0.tar").write_bytes(b"pulled in the UI")  # foreign name: never touched
    pve.digests["ghcr.io/o/web:1.0"] = D_OLD
    t1 = resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None)
    pull(pve, "pve1", t1)
    pve.digests["ghcr.io/o/web:1.0"] = D_NEW
    t2 = resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None)
    pull(pve, "pve1", t2)
    names = sorted(f.name for f in tdir.iterdir())
    assert names == ["ghcr.io_o_web_1.0.tar", f"ghcr.io_o_web_1.0_sha256-{'b' * 16}.tar"]


def test_keep_templates_env_disables_pruning(tmp_path, setup, monkeypatch):
    from pve_oci_compose.create import pull, resolve_template

    monkeypatch.setenv("PVE_OCI_KEEP_TEMPLATES", "1")
    pve, _ = setup
    for d in (D_OLD, D_NEW):
        pve.digests["ghcr.io/o/web:1.0"] = d
        pull(pve, "pve1", resolve_template(pve, "pve1", "ghcr.io/o/web:1.0", None))
    assert len(list(Path(pve.vztmpl_dir("local")).iterdir())) == 2


# ---- cleanup of the temp CT on every failure path -----------------------------------------

def test_shutdown_failure_removes_temp_ct(tmp_path, monkeypatch):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    monkeypatch.setattr(pve, "shutdown_or_stop", lambda vmid, timeout: False)
    with pytest.raises(ComposeError, match="could not shut down"):
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert pve.cmds("pct create 200") and 200 not in pve.cts
    assert not pve.cmds("pct snapshot") and not pve.cmds("rsync") and not pve.cmds("pct rollback")


def test_rsync_failure_unmounts_before_destroying_temp(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    pve.fail["rsync"] = 23
    with pytest.raises(ComposeError):
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    calls = [" ".join(c) for c in pve.calls]
    assert calls.index("pct unmount 200") < calls.index("pct destroy 200 --purge")
    assert 200 not in pve.cts


def test_sighup_during_rsync_cleans_up_and_rolls_back(tmp_path):
    from pve_oci_compose.errors import Terminated

    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    pve.raise_on["rsync"] = Terminated(1)  # SSH session dropped mid-rsync
    with pytest.raises(Terminated):  # original cause propagates (exit 129), not a generic error
        commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert 200 not in pve.cts and pve.cmds("pct rollback 105")
    assert pve.cts[105]["status"] == "running"


def test_signal_during_apply_pct_create_removes_partial_ct(setup, monkeypatch):
    from pve_oci_compose.errors import Terminated

    pve, p = setup
    real_run = pve.run

    def run(argv, **kw):
        if argv[:2] == ["pct", "create"]:
            real_run(argv, **kw)  # the CT exists half-made…
            raise Terminated(15)  # …when SIGTERM arrives
        return real_run(argv, **kw)

    monkeypatch.setattr(pve, "run", run)
    with pytest.raises(Terminated):
        commands.apply(pve, C.load(p), commands.Options())
    assert 200 not in pve.cts and "vmid: next" in p.read_text()


def test_leftover_temp_ct_is_reported(tmp_path, capsys):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path, ref="ghcr.io/o/web:1.0")
    pve.add_ct(250, ["hostname: oci-refresh-temp", "tags: pve-oci-compose-temp"])
    commands.plan(pve, _refresh_compose(tmp_path), commands.Options())
    assert "CT 250 is a leftover refresh temp CT" in capsys.readouterr().err
    assert 250 in pve.cts  # reported, never auto-deleted


def test_temp_ct_is_tagged(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    create = pve.cmds("pct create 200")[0]
    assert create[create.index("--tags") + 1] == "pve-oci-compose-temp"


# ---- privileged CTs (Proxmox cannot unpack OCI images into them directly) -------------------

PRIV = COMPOSE.replace("    start: true\n", "    start: true\n    unprivileged: false\n    protection: true\n")


def test_apply_privileged_converts_via_vzdump_restore(tmp_path, chown_calls):
    p = tmp_path / "compose.yaml"
    p.write_text(PRIV)
    pve = FakePve(tmp_path)
    commands.apply(pve, C.load(p), commands.Options())
    create = pve.cmds("pct create 200")[-1]  # [0] is the direct attempt that hits the Proxmox bug
    assert create[create.index("--unprivileged") + 1] == "1" and "--protection" not in create
    vz = pve.cmds("vzdump 200")[0]
    restore = pve.cmds("pct restore 200")[0]
    assert vz[vz.index("--mode") + 1] == "stop"
    assert restore[4:] [:4] == ["--force", "1", "--unprivileged", "0"]
    assert "--net0" not in restore and "--hostname" not in restore
    assert restore[restore.index("--protection") + 1] == "1"
    # order: create → vzdump → restore → marker/inject → start
    calls = [" ".join(c) for c in pve.calls]
    idx = lambda pre: next(i for i, c in enumerate(calls) if c.startswith(pre))
    assert idx("pct create") < idx("vzdump") < idx("pct restore") < idx("pct mount 200") < idx("pct start 200")
    conf = (ctconf.CONF_DIR / "200.conf").read_text()
    assert "unprivileged:" not in conf and "protection: 1" in conf
    assert "env: PATH=/usr/bin\0IMAGE_VAR=1\0APP=1" in conf  # post-create env survived the restore
    # privileged → injected files keep real ids (no +100000 shift)
    owners = {os.path.relpath(path, tmp_path / "lxc/200/rootfs"): (u, g) for path, u, g in chown_calls}
    assert owners["etc/app.conf"] == (0, 0)
    assert (tmp_path / "dumps").is_dir() and not any((tmp_path / "dumps").iterdir())  # archive removed


def test_apply_privileged_restore_failure_removes_ct(tmp_path):
    p = tmp_path / "compose.yaml"
    p.write_text(PRIV)
    pve = FakePve(tmp_path)
    pve.fail["pct restore"] = 255
    with pytest.raises(ComposeError, match="new CT removed"):
        commands.apply(pve, C.load(p), commands.Options())
    assert 200 not in pve.cts and "vmid: next" in p.read_text()
    assert not any((tmp_path / "dumps").iterdir())  # archive removed even on failure


def test_refresh_privileged_target_gets_privileged_temp(tmp_path):
    pve = FakePve(tmp_path)
    root = pve.add_ct(105, ["hostname: web", "rootfs: local-zfs:subvol-105-disk-0,size=8G",
                            "tags: pve-oci-compose"], status="running")  # no unprivileged: line = privileged
    (root / "etc/pve-oci-compose.json").write_text(json.dumps({"service": "web", "ref": "ghcr.io/o/web:0.9"}))
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    calls = [" ".join(c) for c in pve.calls]
    idx = lambda pre: next(i for i, c in enumerate(calls) if c.startswith(pre))
    # temp CT converted to privileged *before* the target goes down (no extra downtime)
    assert idx("pct create 200") < idx("vzdump 200") < idx("pct restore 200") < idx("pct shutdown 105")
    assert idx("pct restore 200") < idx("rsync")
    assert 200 not in pve.cts


def test_refresh_unprivileged_target_skips_conversion(tmp_path):
    pve = FakePve(tmp_path)
    _managed(pve, tmp_path)  # unprivileged: 1
    commands.refresh(pve, _refresh_compose(tmp_path), commands.Options())
    assert not pve.cmds("vzdump") and not pve.cmds("pct restore")


def test_privileged_create_direct_when_proxmox_is_fixed(tmp_path):
    p = tmp_path / "compose.yaml"
    p.write_text(PRIV)
    pve = FakePve(tmp_path)
    pve.privileged_oci_fixed = True
    commands.apply(pve, C.load(p), commands.Options())
    create = pve.cmds("pct create 200")
    assert len(create) == 1 and create[0][create[0].index("--unprivileged") + 1] == "0"
    assert not pve.cmds("vzdump") and not pve.cmds("pct restore")
    assert "protection: 1" in (ctconf.CONF_DIR / "200.conf").read_text()


def test_privileged_create_tries_direct_first_then_converts(tmp_path):
    p = tmp_path / "compose.yaml"
    p.write_text(PRIV)
    pve = FakePve(tmp_path)
    commands.apply(pve, C.load(p), commands.Options())
    creates = pve.cmds("pct create 200")
    assert [c[c.index("--unprivileged") + 1] for c in creates] == ["0", "1"]


def test_other_privileged_create_errors_do_not_trigger_conversion(tmp_path):
    p = tmp_path / "compose.yaml"
    p.write_text(PRIV)
    pve = FakePve(tmp_path)
    pve.privileged_oci_fixed = True
    pve.fail["pct create"] = 2  # e.g. storage full
    with pytest.raises(ComposeError):
        commands.apply(pve, C.load(p), commands.Options())
    assert len(pve.cmds("pct create")) == 1 and not pve.cmds("vzdump")


def test_plan_shows_registry_version_and_config_drift(tmp_path, capsys):
    pve = FakePve(tmp_path)
    root = _managed(pve, tmp_path, ref="ghcr.io/o/web:stable")
    (root / "etc/pve-oci-compose.json").write_text(json.dumps({"service": "web", "ref": "ghcr.io/o/web:stable",
                                                                "digest": D_OLD, "version": "0.17.1"}))
    pve.digests["ghcr.io/o/web:stable"] = D_NEW
    pve.digests["ghcr.io/o/web:0.18.3"] = D_NEW
    pve.inspects["ghcr.io/o/web"] = {"RepoTags": ["stable", "0.18.3", "0.17.1"], "Labels": {}}
    p = tmp_path / "compose.yaml"
    p.write_text("services:\n  web:\n    vmid: 105\n    image: ghcr.io/o/web:stable\n    rootfs: local-zfs:8\n"
                 "    cores: 4\n")
    commands.plan(pve, C.load(p), commands.Options())
    out = capsys.readouterr().out
    assert "registry now:  0.18.3  (verified tag) · CT has 0.17.1" in out
    assert "config drift:" in out and "cores" in out


def test_refresh_header_and_marker_carry_versions(tmp_path, capsys):
    pve = FakePve(tmp_path)
    root = _managed(pve, tmp_path, ref="ghcr.io/o/web:stable")
    (root / "etc/pve-oci-compose.json").write_text(json.dumps({"service": "web", "ref": "ghcr.io/o/web:stable",
                                                                "digest": D_OLD, "version": "0.17.1"}))
    pve.digests["ghcr.io/o/web:stable"] = D_NEW
    pve.digests["ghcr.io/o/web:0.18.3"] = D_NEW
    pve.inspects["ghcr.io/o/web"] = {"RepoTags": ["stable", "0.18.3"], "Labels": {}}
    commands.refresh(pve, _refresh_compose(tmp_path, "ghcr.io/o/web:stable"), commands.Options())
    assert "stable: 0.17.1 → 0.18.3" in capsys.readouterr().out
    m = json.loads((root / "etc/pve-oci-compose.json").read_text())
    assert (m["digest"], m["version"]) == (D_NEW, "0.18.3")
    assert f"ghcr.io/o/web@{D_NEW}" in " ".join(pve.cmds("skopeo copy")[0])


# ---- architecture: pull the image for this node, ignore other-arch rebuilds ------------------

def test_apply_refuses_image_without_this_platform(tmp_path, setup):
    pve, p = setup
    pve.arches["ghcr.io/o/web:1.0"] = "arm64"
    with pytest.raises(ComposeError, match="has no linux/amd64 image.*linux/arm64"):
        commands.apply(pve, C.load(p), commands.Options())
    assert not pve.cmds("skopeo copy") and not pve.cmds("pct create") and not pve.cts


def test_multi_arch_pull_uses_platform_digest(tmp_path, setup):
    pve, p = setup
    pve.digests["ghcr.io/o/web:1.0"] = "sha256:" + "1" * 64        # index
    pve.platform_digests["ghcr.io/o/web:1.0"] = "sha256:" + "a" * 64  # its linux/amd64 entry
    commands.apply(pve, C.load(p), commands.Options())
    copy = " ".join(pve.cmds("skopeo copy")[0])
    assert f"ghcr.io/o/web@sha256:{'a' * 64}" in copy and f"_sha256-{'a' * 16}.tar" in copy
    m = json.loads((tmp_path / "lxc/200/rootfs/etc/pve-oci-compose.json").read_text())
    assert (m["digest"], m["index_digest"], m["platform"]) == ("sha256:" + "a" * 64, "sha256:" + "1" * 64, "linux/amd64")


def _multi_arch_ct(pve, tmp_path, marker_extra):
    root = _managed(pve, tmp_path, ref="ghcr.io/o/web:stable")
    (root / "etc/pve-oci-compose.json").write_text(json.dumps({"service": "web", "ref": "ghcr.io/o/web:stable",
                                                                **marker_extra}))
    return _refresh_compose(tmp_path, "ghcr.io/o/web:stable")


IDX1, IDX2, AMD1, AMD2 = ("sha256:" + c * 64 for c in "1234")


def test_other_arch_rebuild_does_not_refresh(tmp_path, capsys):
    pve = FakePve(tmp_path)
    c = _multi_arch_ct(pve, tmp_path, {"digest": AMD1, "index_digest": IDX1, "platform": "linux/amd64"})
    pve.digests["ghcr.io/o/web:stable"] = IDX2          # index moved (arm64 rebuilt) …
    pve.platform_digests["ghcr.io/o/web:stable"] = AMD1  # … amd64 entry identical
    commands.refresh(pve, c, commands.Options())
    assert not pve.cmds("rsync")
    assert "only other architectures changed" in capsys.readouterr().out


def test_this_arch_change_refreshes(tmp_path):
    pve = FakePve(tmp_path)
    c = _multi_arch_ct(pve, tmp_path, {"digest": AMD1, "index_digest": IDX1, "platform": "linux/amd64"})
    pve.digests["ghcr.io/o/web:stable"] = IDX2
    pve.platform_digests["ghcr.io/o/web:stable"] = AMD2
    commands.refresh(pve, c, commands.Options())
    assert pve.cmds("rsync")


def test_legacy_index_digest_marker_compat(tmp_path, capsys):
    """Markers from before platform digests store the tag's index digest."""
    pve = FakePve(tmp_path)
    pve.index_contents[IDX1] = AMD1  # what the old index contained for amd64
    c = _multi_arch_ct(pve, tmp_path, {"digest": IDX1})
    pve.digests["ghcr.io/o/web:stable"] = IDX1  # unchanged index → no refresh
    pve.platform_digests["ghcr.io/o/web:stable"] = AMD1
    commands.refresh(pve, c, commands.Options())
    assert not pve.cmds("rsync")
    pve.digests["ghcr.io/o/web:stable"] = IDX2  # index moved, same amd64 image → still no refresh
    commands.refresh(pve, c, commands.Options())
    assert not pve.cmds("rsync") and "only other architectures changed" in capsys.readouterr().out
    pve.platform_digests["ghcr.io/o/web:stable"] = AMD2  # amd64 really changed → refresh
    commands.refresh(pve, c, commands.Options())
    assert pve.cmds("rsync")
