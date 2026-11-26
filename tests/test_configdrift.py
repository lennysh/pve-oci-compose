from pve_oci_compose import compose as C
from pve_oci_compose import configdrift

LIVE = """arch: amd64
cmode: console
cores: 10
entrypoint: /init
env: PATH=/usr/bin\0TZ=America/New_York\0OTHER=x
features: nesting=1,fuse=1
hostname: frigate
memory: 8192
mp0: Storage:subvol-160-disk-1,mp=/config,backup=1,size=3174M
mp1: /mnt/pve/nfs-nvr-data,mp=/media,backup=0,replicate=0,shared=1
nameserver: 10.35.40.2
net0: name=eth0,bridge=vmbr0,firewall=1,gw=10.35.40.1,host-managed=1,hwaddr=BC:24:11:A6:EB:47,ip=10.35.40.3/24,tag=40,type=veth
onboot: 1
protection: 1
rootfs: Storage:subvol-160-disk-0,size=4G
searchdomain: lennysh.net
tags: pve-oci-compose;vlan040
unused0: Storage:subvol-160-disk-2
lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir
lxc.init.cwd: /opt/frigate/
lxc.signal.halt: SIGTERM
lxc.cgroup2.devices.allow: a
lxc.cap.drop: 
[snap1]
cores: 2
""".splitlines()


def svc(**over):
    data = {
        "unprivileged": 0, "onboot": 1, "hostname": "frigate", "memory": 8192, "cores": 10,
        "features": "fuse=1,nesting=1", "nameserver": ["10.35.40.2"], "searchdomain": "lennysh.net",
        "tags": "vlan040", "net0": "name=eth0,bridge=vmbr0,firewall=1,hwaddr=BC:24:11:A6:EB:47,ip=10.35.40.3/24,gw=10.35.40.1,tag=40",
        "mounts": ["Storage:0.1:/config:backup=1"],
        "bind_mounts": ["/mnt/pve/nfs-nvr-data,mp=/media,replicate=0,shared=1,backup=0"],
        "lxc_config_lines": ["lxc.mount.entry: /dev/dri dev/dri none bind,optional,create=dir",
                             "lxc.cgroup2.devices.allow: a", "lxc.cap.drop: "],
    }
    data.update(over)
    return C.Service("app", data, {"TZ": "America/New_York"}, {})


def keys(diffs):
    return [d.key for d in diffs]


def test_matching_compose_has_only_the_detached_disk():
    # grown volume, PVE-added net keys, list vs string, feature order, image env, lxc.init.* → no diffs
    assert keys(configdrift.compare(svc(), LIVE)) == ["unused0"]


def test_detects_frigate_style_drift():
    s = svc(unprivileged=1, cores=8,
            lxc_config_lines=["lxc.cgroup2.devices.allow: a", "lxc.cap.drop: ", "lxc.cgroup2.devices.allow: c 188:* rwm"])
    s.service_env = {"FRIGATE_RTSP_PASSWORD": "secret"}
    d = {x.key: x for x in configdrift.compare(s, LIVE)}
    assert "privileged" in d["unprivileged"].ct
    assert (d["cores"].compose, d["cores"].ct) == ("8", "10")
    assert d["lxc.cgroup2.devices.allow"].ct == "missing"
    assert "only on CT" in d["lxc.mount.entry"].ct
    assert d["env FRIGATE_RTSP_PASSWORD"].ct == "missing"
    assert all("secret" not in x.line() for x in d.values())  # env values never shown


def test_net_mount_and_tag_differences():
    s = svc(net0="name=eth0,bridge=vmbr1,ip=dhcp", mounts=["Storage:8:/config"], tags="vlan040;cams",
            bind_mounts=["/mnt/other,mp=/media"])
    d = {x.key: x for x in configdrift.compare(s, LIVE)}
    assert d["net0.bridge"].compose == "vmbr1" and d["net0.ip"].ct == "10.35.40.3/24"
    assert "smaller" in d["mount /config"].ct  # 3174M < 8G
    assert d["mount /media"].ct == "/mnt/pve/nfs-nvr-data"
    assert "cams" in d["tags"].compose


def test_snapshot_sections_are_ignored():
    assert "cores" not in keys(configdrift.compare(svc(), LIVE))  # [snap1] has cores: 2
