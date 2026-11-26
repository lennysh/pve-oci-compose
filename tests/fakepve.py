"""In-memory stand-in for :class:`pve_oci_compose.pve.Pve` (no Proxmox needed)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from pve_oci_compose import ctconf
from pve_oci_compose.pve import Pve


class FakePve(Pve):
    def __init__(self, tmp: Path, dry_run: bool = False):
        super().__init__(dry_run=dry_run)
        self.tmp = tmp
        self.cts: dict[int, dict] = {}  # vmid -> {"conf": [...], "status": str}
        self.calls: list[list[str]] = []
        self.next_id = 200
        self.fail: dict[str, int] = {}  # argv[0:2] joined ("rsync", "pct create") -> exit code
        self.raise_on: dict[str, BaseException] = {}  # same keys -> exception raised instead
        self.privileged_oci_fixed = False  # pve-container ≤ 6.1.x: privileged OCI create fails
        self.pools: dict[str, list[int]] = {}
        self.qemu: set[int] = set()
        self.digests: dict[str, str | None] = {}  # normalized ref -> registry digest (fake registry)
        self.volids: set[str] = set()  # templates on storage
        self.inspects: dict[str, dict] = {}  # repo -> skopeo inspect result (Labels, RepoTags)
        self.web: dict[str, object] = {}  # url -> JSON (registry web APIs)
        ctconf.CONF_DIR = tmp / "pve-lxc"
        ctconf.CONF_DIR.mkdir(exist_ok=True)

    # helpers for tests
    def add_ct(self, vmid: int, conf: list[str], status: str = "stopped") -> Path:
        self.cts[vmid] = {"status": status}
        (ctconf.CONF_DIR / f"{vmid}.conf").write_text("\n".join(conf) + "\n")
        root = self.tmp / "lxc" / str(vmid) / "rootfs"
        (root / "etc").mkdir(parents=True, exist_ok=True)
        return root

    def cmds(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls if " ".join(c).startswith(prefix)]

    # ---- overrides ----
    def run(self, argv, check=True, stream=False, input_text=None):
        self.calls.append(list(argv))
        key2, key1 = " ".join(argv[:2]), argv[0]
        if (argv[:2] == ["pct", "create"] and not self.privileged_oci_fixed and "--unprivileged" in argv
                and argv[argv.index("--unprivileged") + 1] == "0"):
            msg = f"Detected OCI archive\nunable to create CT {argv[2]} - setgid(0): Invalid argument\n"
            return subprocess.CompletedProcess(argv, 255, msg, None)
        exc = self.raise_on.get(key2) or self.raise_on.get(key1)
        if exc is not None:
            raise exc
        rc = self.fail.get(key2, self.fail.get(key1, 0))
        out = ""
        if rc == 0:
            out = self._apply(argv)
        cp = subprocess.CompletedProcess(argv, rc, out, None)
        if check and rc != 0:
            from pve_oci_compose.errors import CommandError

            raise CommandError(argv, rc, out)
        return cp

    def _apply(self, argv) -> str:
        if argv[:2] == ["pct", "create"]:
            vmid = int(argv[2])
            conf = [f"rootfs: {argv[argv.index('--rootfs') + 1]}"]
            i = 4
            while i < len(argv):
                if argv[i].startswith("--") and i + 1 < len(argv) and argv[i] != "--rootfs":
                    conf.append(f"{argv[i][2:]}: {argv[i + 1]}")
                i += 2
            conf.append("env: PATH=/usr/bin\0IMAGE_VAR=1")
            self.add_ct(vmid, conf)
        elif argv[:2] == ["pct", "destroy"]:
            self.cts.pop(int(argv[2]), None)
        elif argv[:2] == ["pct", "start"]:
            self.cts[int(argv[2])]["status"] = "running"
        elif argv[:2] in (["pct", "stop"], ["pct", "shutdown"]):
            self.cts[int(argv[2])]["status"] = "stopped"
        elif argv[:2] == ["pct", "set"] and "--tags" in argv:
            self._set_conf(int(argv[2]), "tags", argv[argv.index("--tags") + 1])
        elif argv[:2] == ["pct", "set"] and "--entrypoint" in argv:
            self._set_conf(int(argv[2]), "entrypoint", argv[argv.index("--entrypoint") + 1])
        elif argv[:3] == ["pvesh", "create", "/pools"]:
            self.pools.setdefault(argv[argv.index("--poolid") + 1], [])
        elif argv[:2] == ["pvesh", "set"] and argv[2].startswith("/pools/"):
            self.pools.setdefault(argv[2][len("/pools/"):], []).append(int(argv[argv.index("--vms") + 1]))
        elif argv[:2] == ["pvesh", "create"] and argv[2].endswith("/oci-registry-pull"):
            st = argv[2].split("/")[4]
            ref = argv[argv.index("--reference") + 1]
            self.volids.add(f"{st}:vztmpl/{self.normalize_content_filename(ref)}.tar")
            return '"UPID:pve1:0001:task:"'
        elif argv[0] == "vzdump" and "--dumpdir" in argv:
            d = Path(argv[argv.index("--dumpdir") + 1])
            d.mkdir(parents=True, exist_ok=True)
            (d / f"vzdump-lxc-{argv[1]}-2026_01_01-00_00_00.tar.zst").write_bytes(b"fake")
        elif argv[:2] == ["pct", "restore"]:
            vmid = int(argv[2])
            p = ctconf.CONF_DIR / f"{vmid}.conf"
            lines = p.read_text().splitlines()
            flags = dict(zip(argv[4::2], argv[5::2]))
            if flags.get("--unprivileged") == "0":  # real config simply has no unprivileged: line
                lines = [line for line in lines if not line.startswith("unprivileged:")]
            for f, v in flags.items():
                key = f[2:]
                if key in ("force", "unprivileged"):
                    continue
                lines = [line for line in lines if not line.startswith(f"{key}:")] + [f"{key}: {v}"]
            p.write_text("\n".join(lines) + "\n")
        elif argv[:2] == ["pvesh", "create"] and argv[2].endswith("/vzdump"):
            return '"UPID:pve1:0002:task:"'
        elif argv[:2] == ["pvesm", "free"]:
            self.volids.discard(argv[2])
            st, _, rest = argv[2].partition(":vztmpl/")
            (Path(self.vztmpl_dir(st)) / rest).unlink(missing_ok=True)
        elif argv[:2] == ["skopeo", "copy"]:
            dest = next(a for a in argv if a.startswith("oci-archive:"))[len("oci-archive:"):]
            Path(dest).write_bytes(b"oci archive of " + argv[-2].encode())
        return ""

    def _set_conf(self, vmid: int, key: str, val: str) -> None:
        p = ctconf.CONF_DIR / f"{vmid}.conf"
        lines = [line for line in p.read_text().splitlines() if not line.startswith(f"{key}:")]
        lines.insert(0, f"{key}: {val}")
        p.write_text("\n".join(lines) + "\n")

    def mutate(self, argv, stream=False, check=True):
        if self.dry_run:
            self.calls.append(["DRY-RUN", *argv])
            return None
        return self.run(argv, check=check, stream=stream)

    def out(self, argv):
        return None

    def pvesh_get(self, path, *params):
        return None

    def node_name(self):
        return "pve1"

    def nextid(self):
        while self.next_id in self.cts:
            self.next_id += 1
        return self.next_id

    def cluster_guests(self):
        def tags(v):
            cfg = (ctconf.CONF_DIR / f"{v}.conf").read_text().splitlines()
            return next((line[5:].strip() for line in cfg if line.startswith("tags:")), "")

        return [{"type": "lxc", "vmid": v, "node": "pve1", "tags": tags(v)} for v in self.cts] + [
            {"type": "qemu", "vmid": v, "node": "pve1"} for v in self.qemu
        ]

    def config_lines(self, vmid):
        if vmid not in self.cts:
            return None
        return (ctconf.CONF_DIR / f"{vmid}.conf").read_text().splitlines()

    def status(self, vmid):
        return self.cts.get(vmid, {}).get("status", "")

    def pid(self, vmid):
        return None  # force the pct mount path

    def rootfs_mountpoint(self, vmid):
        return str(self.tmp / "lxc" / str(vmid) / "rootfs")

    def mount(self, vmid):
        self.calls.append(["pct", "mount", str(vmid)])
        return self.rootfs_mountpoint(vmid)

    def unmount(self, vmid):
        self.calls.append(["pct", "unmount", str(vmid)])

    def storages(self, node):
        return [{"storage": "local", "type": "dir", "content": "vztmpl,iso"},
                {"storage": "local-zfs", "type": "zfspool", "content": "rootdir,images"}]

    def storage_has_volid(self, node, storage, volid):
        return volid in self.volids or (Path(self.vztmpl_dir(storage)) / volid.split("vztmpl/", 1)[1]).is_file()

    def registry_digest(self, ref):
        if "@sha256:" in ref:
            return ref.split("@", 1)[1]
        return self.digests.get(ref, "sha256:" + "0" * 64)

    def normalize_content_filename(self, ref):
        return ref.replace("/", "_").replace(":", "_")

    def vztmpl_dir(self, storage):
        d = self.tmp / "vztmpl" / storage
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    def skopeo_inspect(self, ref):
        return self.inspects.get(ref.split("@")[0].rsplit(":", 1)[0], {"RepoTags": [], "Labels": {}})

    def http_json(self, url, timeout=10.0):
        return self.web.get(url)

    def wait_task(self, upid, node=None, max_wait=7200):
        return None

    def pvesm_path(self, volid):
        return None

    def pool_exists(self, pool):
        return pool in self.pools

    def pool_members(self, pool):
        return [{"type": "lxc", "vmid": v} for v in self.pools.get(pool, [])]
