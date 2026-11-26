import pytest

from pve_oci_compose import compose as C
from pve_oci_compose import ctconf, refs
from pve_oci_compose.create import oci_storages, spec_from_service
from pve_oci_compose.errors import ComposeError
from pve_oci_compose.refresh import rootfs_alloc, rsync_excludes

# ---- compose ----------------------------------------------------------------------

YAML = """\
name: media
defaults:
  rootfs: local-zfs:8
  env: {TZ: UTC, A: default}
services:
  web:   # the web thing
    vmid: next   # allocated
    image: nginx:1.27
    environment: ["A=svc", "B=2"]
  db:
    vmid: 105
    image: postgres:16
    pool: ""
"""


def write(tmp_path, text=YAML):
    p = tmp_path / "compose.yaml"
    p.write_text(text)
    return p


def test_load_merges_defaults_and_env_layers(tmp_path):
    c = C.load(write(tmp_path))
    web, db = c.services
    assert [s.name for s in c.services] == ["web", "db"]  # file order
    assert web.get("rootfs") == "local-zfs:8"
    assert web.defaults_env == {"TZ": "UTC", "A": "default"}
    assert web.service_env == {"A": "svc", "B": "2"}
    assert web.env == {"TZ": "UTC", "A": "svc", "B": "2"}
    assert web.vmid_spec == "next" and db.vmid == 105
    assert web.pool(c.project) == "media" and db.pool(c.project) is None


@pytest.mark.parametrize("v,exp", [(None, "next"), ("auto", "next"), ("NEXT", "next"), (7, 7), ("12", 12)])
def test_vmid_spec(v, exp):
    assert C.Service("s", {"vmid": v}).vmid_spec == exp


def test_vmid_spec_invalid():
    with pytest.raises(ComposeError):
        C.Service("s", {"vmid": "abc"}).vmid_spec


def test_rewrite_vmid_keeps_comments_and_only_touches_target():
    out = C.rewrite_vmid(YAML, "web", 201)
    assert "    vmid: 201 # allocated\n" in out
    assert "vmid: 105" in out
    assert out.replace("    vmid: 201 # allocated", "    vmid: next   # allocated") == YAML


def test_rewrite_vmid_errors():
    with pytest.raises(ComposeError, match="no services.nope"):
        C.rewrite_vmid(YAML, "nope", 1)
    with pytest.raises(ComposeError, match="no vmid"):
        C.rewrite_vmid("services:\n  a:\n    image: x\n  b:\n    vmid: next\n", "a", 1)
    with pytest.raises(ComposeError):
        C.rewrite_vmid(YAML, "bad name", 1)


def test_validate_rejects_dotdot_and_bad_lines(tmp_path):
    base = {"image": "x", "rootfs": "s:8"}
    with pytest.raises(ComposeError, match=r"\.\."):
        C.validate(C.Service("s", {**base, "rootfs_files": [{"path": "/etc/../x", "content": ""}]}), tmp_path)
    with pytest.raises(ComposeError, match="lxc_config_lines"):
        C.validate(C.Service("s", {**base, "lxc_config_lines": ["# nope"]}), tmp_path)
    with pytest.raises(ComposeError, match="bind_mounts"):
        C.validate(C.Service("s", {**base, "bind_mounts": ["relative,mp=/x"]}), tmp_path)


def test_parse_mount_spec():
    assert C.parse_mount_spec("local-zfs:8:/data") == "local-zfs:8,mp=/data,backup=1"
    assert C.parse_mount_spec("s:2:/d:backup=0,ro=1") == "s:2,mp=/d,backup=0,ro=1"
    with pytest.raises(ComposeError):
        C.parse_mount_spec("s:0:/d")


# ---- refs --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref,exp",
    [
        ("nginx:1", "docker.io/library/nginx:1"),
        ("user/app:2", "docker.io/user/app:2"),
        ("ghcr.io/o/app:3", "ghcr.io/o/app:3"),
        ("registry:5000/img:1", "registry:5000/img:1"),  # port means registry host
        ("localhost/img", "localhost/img"),
        ("oci://docker.io/x/y:1", "docker.io/x/y:1"),
    ],
)
def test_normalize(ref, exp):
    assert refs.normalize(ref) == exp


def test_tag_candidates_order():
    tags = ["latest", "1.9", "1.10", "1.10-alpine", "sha256-abc.sig", "1.10.sig", "v2.0", "mainline"]
    assert refs.tag_candidates(tags) == ["v2.0", "1.10", "1.9", "1.10-alpine", "mainline"]
    # most specific first; dotted versions before date-style snapshots
    assert refs.tag_candidates(["3.24", "3.24.1", "20260805", "3.9.9", "edge"]) == [
        "3.24.1", "3.24", "3.9.9", "20260805", "edge"]
    assert refs.tag_candidates(["latest"]) == []


class _Reg:
    """Minimal registry: RepoTags lists every tag of the repo, digests differ per tag."""

    def __init__(self, digests):
        self.digests = digests
        self.probes = []

    def skopeo_inspect(self, ref):
        return {"Name": "docker.io/library/nginx", "Digest": self.digests["latest"], "RepoTags": list(self.digests)}

    def registry_digest(self, ref):
        self.probes.append(ref)
        return self.digests.get(ref.rsplit(":", 1)[1])


def test_resolve_floating_verifies_digest_not_just_highest_tag():
    # highest tag 1.29.0 is a *different* image than latest (= 1.28.2)
    reg = _Reg({"latest": "sha256:L", "1.29.0": "sha256:X", "1.28.2": "sha256:L", "1.27.0": "sha256:Y"})
    assert refs.resolve_floating(reg, "docker.io/library/nginx:latest", "sha256:L") == "docker.io/library/nginx:1.28.2"


def test_resolve_floating_falls_back_to_digest():
    reg = _Reg({"latest": "sha256:L", **{f"1.{i}.0": f"sha256:{i}" for i in range(20)}})
    out = refs.resolve_floating(reg, "docker.io/library/nginx:latest", "sha256:L")
    assert out == "docker.io/library/nginx@sha256:L"
    assert len(reg.probes) == refs.MAX_TAG_PROBES


# ---- create args ------------------------------------------------------------------------


def test_spec_from_service_flags():
    svc = C.Service("web", {
        "rootfs": "local-zfs:8", "hostname": "web", "net1": "name=eth1,bridge=vmbr1", "net0": "name=eth0,bridge=vmbr0",
        "memory": 512, "onboot": True, "unprivileged": True, "nameserver": "1.1.1.1, 9.9.9.9",
        "searchdomain": ["a.lan", "b.lan"], "password": "pw", "mounts": ["local-zfs:4:/data"],
        "bind_mounts": ["/mnt/x,mp=/x"], "lxc_dev": ["/dev/dri/renderD128"], "protection": False, "start": True,
        "lxc_config_lines": ["lxc.cgroup2.devices.allow: c 226:* rwm"],
    })
    s = spec_from_service(svc, 101, "media", "desc")
    a = s.args
    assert a[:4] == ["--net0", "name=eth0,bridge=vmbr0", "--net1", "name=eth1,bridge=vmbr1"]
    pairs = dict(zip(a[::2], a[1::2]))
    assert pairs["--memory"] == "512" and pairs["--onboot"] == "1" and pairs["--unprivileged"] == "1"
    assert pairs["--nameserver"] == "1.1.1.1 9.9.9.9" and pairs["--searchdomain"] == "a.lan b.lan"
    assert pairs["--mp0"] == "local-zfs:4,mp=/data,backup=1" and pairs["--mp1"] == "/mnt/x,mp=/x"
    assert pairs["--dev0"] == "/dev/dri/renderD128" and pairs["--protection"] == "0"
    assert pairs["--pool"] == "media" and pairs["--description"] == "desc"
    assert "--start" not in a and s.start  # started by apply after marker/inject
    assert s.lxc_lines == ["lxc.cgroup2.devices.allow: c 226:* rwm"]


def test_spec_defaults():
    a = spec_from_service(C.Service("x", {"rootfs": "s:8"}), 300, None, None).args
    pairs = dict(zip(a[::2], a[1::2]))
    assert pairs["--net0"].startswith("name=eth0") and pairs["--hostname"] == "oci-ct-300"
    assert pairs["--unprivileged"] == "1" and pairs["--onboot"] == "0"


def test_oci_storages():
    assert oci_storages([
        {"storage": "local", "type": "dir", "content": "vztmpl"},
        {"storage": "nfs", "type": "nfs", "content": "backup,vztmpl"},
        {"storage": "zfs", "type": "zfspool", "content": "vztmpl"},
        {"storage": "iso", "type": "dir", "content": "iso"},
    ]) == ["local", "nfs"]


# ---- ct config edits -------------------------------------------------------------------


CONF = "arch: amd64\nenv: PATH=/bin\0A=image\nhostname: web\n\n[snap1]\nenv: OLD=1\n"


def test_merge_env_layers_and_keeps_snapshots():
    out = ctconf.merge_env_text(CONF, [{"A": "default"}, {"B": "svc"}])
    cur, snap = out.split("[snap1]")
    assert "env: PATH=/bin\0A=default\0B=svc\n" in cur
    assert snap == "\nenv: OLD=1\n"
    assert ctconf.merge_env_text(CONF, [{}, {}]) is None


def test_merge_env_replace():
    out = ctconf.merge_env_text(CONF, [{"NEW": "img"}, {"B": "svc"}], keep_existing=False)
    assert "env: NEW=img\0B=svc\n" in out and "PATH=/bin" not in out.split("[snap1]")[0]


def test_merge_env_rejects_newlines():
    with pytest.raises(ValueError):
        ctconf.merge_env_text(CONF, [{"A": "x\ny"}])


def test_append_lines_drops_legacy_block():
    text = "a: 1\n# BEGIN pve-oci-compose lxc_config_lines\nold: x\n# END pve-oci-compose lxc_config_lines\n"
    assert ctconf.append_lines_text(text, ["lxc.x: y"]) == "a: 1\nlxc.x: y\n"


# ---- refresh helpers ----------------------------------------------------------------------


@pytest.mark.parametrize("line,exp", [
    ("local-zfs:subvol-100-disk-0,size=8G", "local-zfs:8"),
    ("local-lvm:vm-100-disk-0,size=512M", "local-lvm:1"),
    ("local:100/vm-100-disk-0.raw,size=2T", "local:2048"),
])
def test_rootfs_alloc(line, exp):
    assert rootfs_alloc(line) == exp


def test_rsync_excludes_are_anchored():
    lines = ["mp0: local:vol,mp=/data,backup=1", "mp1: /mnt/nfs,mp=/mnt/media/,shared=1", "[snap]", "mp2: x,mp=/old"]
    assert rsync_excludes(lines) == ["--exclude=/data", "--exclude=/mnt/media"]


def test_marker_roundtrip_with_and_without_digest():
    from pve_oci_compose import marker

    assert marker.parse(marker.encode("web", "x:1", "sha256:d")) == marker.Marker("web", "x:1", "sha256:d")
    assert marker.parse(b'{"service":"web","ref":"x:1"}') == marker.Marker("web", "x:1", None)
    assert b"digest" not in marker.encode("web", "x:1")


def test_ui_survives_dead_terminal(monkeypatch):
    import sys

    from pve_oci_compose import ui

    class Dead:
        def write(self, *_):
            raise OSError(5, "Input/output error")  # EIO after the SSH session is gone

        def flush(self):
            raise OSError(5, "Input/output error")

    monkeypatch.setattr(sys, "stdout", Dead())
    monkeypatch.setattr(sys, "stderr", Dead())
    ui.say("x"); ui.err("y"); ui.warn("z"); ui.ok("w"); ui.title("t")  # must not raise


def test_signal_handlers_raise_terminated():
    import os
    import signal

    from pve_oci_compose.cli import install_signal_handlers
    from pve_oci_compose.errors import Terminated

    old = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGHUP)}
    try:
        install_signal_handlers()
        with pytest.raises(Terminated) as ei:
            os.kill(os.getpid(), signal.SIGHUP)
        assert ei.value.signum == signal.SIGHUP
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def test_privileged_plan_restore_gets_only_volumes():
    from pve_oci_compose.create import privileged_plan

    svc = C.Service("x", {
        "rootfs": "Storage:4", "unprivileged": False, "protection": True, "ha_managed": True,
        "net0": "name=eth0,bridge=vmbr0,ip=dhcp", "mounts": ["Storage:2:/data"],
        "bind_mounts": ["/mnt/nfs,mp=/media"], "memory": 512,
    })
    spec = spec_from_service(svc, 300, None, None)
    assert spec.privileged
    create, volumes, extra = privileged_plan(spec)
    cpairs = dict(zip(create[::2], create[1::2]))
    assert cpairs["--unprivileged"] == "1"  # Proxmox can only unpack OCI unprivileged
    assert "--protection" not in cpairs and "--ha-managed" not in cpairs  # would block the restore
    assert cpairs["--mp0"] == "Storage:2,mp=/data,backup=1" and cpairs["--memory"] == "512"
    # restore: volumes only — re-passing net0 would drop OCI's host-managed=1 and the MAC
    assert volumes == ["--rootfs", "Storage:4", "--mp0", "Storage:2,mp=/data,backup=1", "--mp1", "/mnt/nfs,mp=/media"]
    assert extra == ["--protection", "1", "--ha-managed", "1"]


def test_unprivileged_spec_is_not_converted():
    assert not spec_from_service(C.Service("x", {"rootfs": "s:1"}), 1, None, None).privileged
