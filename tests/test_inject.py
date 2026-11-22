import os

import pytest

from pve_oci_compose.guestfs import GuestPathError
from pve_oci_compose.inject import IdMap, idmap_from_config, parse_mp_lines, resolve_target, run


def test_idmap_default_unprivileged_when_config_silent():
    im = idmap_from_config([], unprivileged_default=True)
    assert im.uid(0) == 100000
    assert im.uid(1000) == 101000
    assert im.uid(100033) == 100033  # legacy host id passes through


def test_idmap_readable_config_without_unprivileged_is_privileged():
    im = idmap_from_config(["hostname: x", "rootfs: s:1"], unprivileged_default=True)
    assert im.uid(0) == 0


def test_idmap_privileged_identity():
    im = idmap_from_config(["unprivileged: 0"], unprivileged_default=True)
    assert im.uid(0) == 0 and im.gid(33) == 33


def test_idmap_custom_ranges():
    lines = [
        "unprivileged: 1",
        "lxc.idmap: u 0 100000 1000",
        "lxc.idmap: u 1000 1000 1",
        "lxc.idmap: u 1001 101001 64535",
        "lxc.idmap: g 0 100000 65536",
    ]
    im = idmap_from_config(lines)
    assert im.uid(1000) == 1000
    assert im.uid(1001) == 101001
    assert im.gid(1000) == 101000


def test_idmap_uncovered_raises():
    im = IdMap({"u": [(0, 100000, 10)], "g": [(0, 100000, 10)]})
    with pytest.raises(ValueError):
        im.uid(50)


def test_parse_mp_lines():
    lines = [
        "mp0: local-zfs:subvol-101-disk-1,mp=/data,backup=1,size=8G",
        "mp1: /mnt/pve/nfs,mp=/mnt/media,shared=1",
        "rootfs: local-zfs:subvol-101-disk-0,size=8G",
    ]
    mps = parse_mp_lines(lines, lambda v: f"/pool/{v}")
    assert mps == {
        "/data": ("volume", "/pool/local-zfs:subvol-101-disk-1"),
        "/mnt/media": ("bind", "/mnt/pve/nfs"),
    }


def test_resolve_target_prefers_dir_backed_base(tmp_path):
    base = tmp_path / "vol"
    base.mkdir()
    mps = {"/data": ("volume", str(base)), "/raw": ("volume", "/dev/pve/vm-1-disk-1")}
    assert resolve_target("/data/app/x.conf", "/R", mps) == (str(base), "/app/x.conf")
    # block-device volume: go through the pct-mounted rootfs
    assert resolve_target("/raw/x", "/R", mps) == ("/R", "/raw/x")
    with pytest.raises(GuestPathError):
        resolve_target("/elsewhere/x", "/R", mps)
    with pytest.raises(GuestPathError):
        resolve_target("/data", "/R", mps)


def _privileged_run(doc, tmp_path, phase="apply", lines=("unprivileged: 0",)):
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir(exist_ok=True)
    return rootfs, run(phase, doc, str(tmp_path), str(rootfs), list(lines), lambda v: None)


def test_run_apply_and_refresh_phases(tmp_path):
    (tmp_path / "src.txt").write_text("from source\n")
    doc = {
        "rootfs_files": [
            {"path": "/etc/a.conf", "content": "A\n", "mode": "0600"},
            {"path": "/etc/b.conf", "source": "src.txt", "on_refresh": True},
        ]
    }
    rootfs, done = _privileged_run(doc, tmp_path, "apply")
    assert done == [("rootfs", "/etc/a.conf"), ("rootfs", "/etc/b.conf")]
    assert (rootfs / "etc/a.conf").read_text() == "A\n"
    assert (rootfs / "etc/b.conf").read_text() == "from source\n"
    assert (os.stat(rootfs / "etc/a.conf").st_mode & 0o777) == 0o600

    (rootfs / "etc/a.conf").write_text("changed")
    _, done = _privileged_run(doc, tmp_path, "refresh")
    assert done == [("rootfs", "/etc/b.conf")]
    assert (rootfs / "etc/a.conf").read_text() == "changed"


def test_run_refuses_symlink_planted_in_volume(tmp_path):
    host_secret = tmp_path / "secret"
    host_secret.write_text("HOST\n")
    vol = tmp_path / "vol"
    vol.mkdir()
    os.symlink(str(host_secret), vol / "app.conf")
    doc = {"mount_files": [{"mount": "/data", "path": "app.conf", "content": "x", "on_refresh": True}]}
    lines = ["unprivileged: 0", "mp0: local:vol,mp=/data"]
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    with pytest.raises(GuestPathError):
        run("refresh", doc, str(tmp_path), str(rootfs), lines, lambda v: str(vol))
    assert host_secret.read_text() == "HOST\n"


def test_unprivileged_ownership_is_mapped(tmp_path, chown_calls):
    doc = {"rootfs_files": [{"path": "/etc/app/a", "content": "x", "owner": 1000, "group": 1000}]}
    rootfs = tmp_path / "rootfs"
    rootfs.mkdir()
    run("apply", doc, str(tmp_path), str(rootfs), [], lambda v: None)  # no unprivileged: line
    owners = {os.path.relpath(p, rootfs): (u, g) for p, u, g in chown_calls}
    assert owners["etc/app/a"] == (101000, 101000)
    assert owners["etc"] == (100000, 100000)  # created dirs belong to guest root
