"""Compare what a compose service declares with a CT's live config (read-only report).

Most settings are create-only (refresh keeps the CT's host config), so a compose file can
drift from reality — e.g. after changes made in the UI. ``plan`` shows the differences;
refresh warns about them. Nothing is changed here.

Only keys the compose service actually sets are compared (after defaults merge), plus
``unprivileged`` (create default 1). Values PVE derives from the image at create time
(entrypoint, lxc.init.*, lxc.signal.halt, image env) are not compose's business.
Env values are never printed — only whether a key is missing or different.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .compose import Service, as_flag, as_scalar, parse_mount_spec
from .inject import truthy
from .pve import MARKER_TAG, TEMP_TAG, parse_config, split_tags

IMAGE_DERIVED_LXC = {"lxc.init.cwd", "lxc.init.uid", "lxc.init.gid", "lxc.init.groups", "lxc.signal.halt"}
SCALARS = ["hostname", "memory", "swap", "cores", "cpulimit", "cpuunits", "onboot", "protection",
           "timezone", "startup", "arch", "ostype", "cmode", "console", "tty"]


@dataclass
class Diff:
    key: str
    compose: str
    ct: str

    def line(self) -> str:
        return f"{self.key:<26} compose {self.compose or '—'} · CT {self.ct or '—'}"


def _kv(spec: str) -> dict[str, str]:
    out = {}
    for part in spec.split(","):
        k, sep, v = part.partition("=")
        out[k.strip()] = v.strip() if sep else ""
    return out


def _lxc_line(s: str) -> tuple[str, str]:
    m = re.match(r"^\s*([A-Za-z0-9_.]+)\s*[:=]\s?(.*)$", s)
    return (m.group(1), m.group(2).strip()) if m else (s.strip(), "")


def _features(v: str) -> str:
    return ",".join(sorted(p.strip() for p in str(v).split(",") if p.strip()))


def _norm_list(v) -> str:
    if isinstance(v, list):
        return " ".join(str(x) for x in v)
    return " ".join(x for x in re.split(r"[,\s]+", str(v)) if x)


def _size_gib(s: str) -> float | None:
    m = re.search(r"(?:^|,)size=(\d+(?:\.\d+)?)([KMGT]?)", s)
    if not m:
        return None
    n, u = float(m.group(1)), m.group(2) or "G"
    return {"K": n / 1024**2, "M": n / 1024, "G": n, "T": n * 1024}[u]


def compare(svc: Service, lines: list[str]) -> list[Diff]:
    if not lines:
        return []
    cur = []
    for line in lines:
        if line.startswith("["):
            break
        cur.append(line)
    cfg = parse_config(cur)
    out: list[Diff] = []

    # privilege (create default: unprivileged)
    want_unpriv = as_flag(svc.get("unprivileged")) or "1"
    have_unpriv = "1" if truthy(cfg.get("unprivileged")) else "0"
    if want_unpriv != have_unpriv:
        name = {"1": "unprivileged", "0": "privileged"}
        out.append(Diff("unprivileged", f"{want_unpriv} ({name[want_unpriv]})", f"{have_unpriv} ({name[have_unpriv]})"))

    for key in SCALARS:
        if key not in svc.data or svc.get(key) is None:
            continue
        want = as_flag(svc.get(key)) if isinstance(svc.get(key), bool) else as_scalar(svc.get(key))
        have = cfg.get(key, "")
        if key in ("onboot", "protection"):
            have = have or "0"
        if (want or "") != have:
            out.append(Diff(key, want or "", have))

    if svc.get("features") is not None and _features(svc.get("features")) != _features(cfg.get("features", "")):
        out.append(Diff("features", _features(svc.get("features")), _features(cfg.get("features", ""))))
    for key in ("nameserver", "searchdomain"):
        if svc.get(key) is not None and _norm_list(svc.get(key)) != _norm_list(cfg.get(key, "")):
            out.append(Diff(key, _norm_list(svc.get(key)), _norm_list(cfg.get(key, ""))))
    if svc.get("tags"):
        want = {t for t in split_tags(str(svc.get("tags")))}
        have = {t for t in split_tags(cfg.get("tags", ""))} - {MARKER_TAG, TEMP_TAG}
        if want - have:
            out.append(Diff("tags", ",".join(sorted(want)), ",".join(sorted(have))))

    # networks: only the keys compose sets (hwaddr/type/host-managed are added by PVE)
    for key in sorted((k for k in svc.data if re.fullmatch(r"net\d+", k)), key=lambda k: int(k[3:])):
        want, have = _kv(str(svc.data[key])), _kv(cfg.get(key, ""))
        if key not in cfg:
            out.append(Diff(key, str(svc.data[key]), ""))
            continue
        for k, v in want.items():
            if have.get(k, "") != v:
                out.append(Diff(f"{key}.{k}", v, have.get(k, "")))

    # mount points, matched by guest path
    live_mps: dict[str, str] = {}
    for k, v in cfg.items():
        if re.fullmatch(r"mp\d+", k):
            m = re.search(r"(?:^|,)mp=([^,]+)", v)
            if m:
                live_mps[m.group(1).rstrip("/") or "/"] = v
    want_mps: dict[str, tuple[str, str]] = {}
    for spec in svc.strings("mounts"):
        val = parse_mount_spec(spec, svc.name)
        path = re.search(r",mp=([^,]+)", val).group(1).rstrip("/") or "/"
        want_mps[path] = ("volume", val)
    for spec in svc.strings("bind_mounts"):
        m = re.search(r",mp=([^,]+)", spec)
        if m:
            want_mps[m.group(1).rstrip("/") or "/"] = ("bind", spec)
    for path, (kind, val) in want_mps.items():
        have = live_mps.get(path)
        if have is None:
            out.append(Diff(f"mount {path}", val, "missing"))
            continue
        if kind == "bind":
            if val.split(",")[0] != have.split(",")[0]:
                out.append(Diff(f"mount {path}", val.split(",")[0], have.split(",")[0]))
        else:
            if have.startswith("/"):
                out.append(Diff(f"mount {path}", "volume", "bind " + have.split(",")[0]))
            elif val.split(":")[0] != have.split(":")[0]:
                out.append(Diff(f"mount {path}", f"storage {val.split(':')[0]}", f"storage {have.split(':')[0]}"))
            else:
                want_gib = float(val.split(",")[0].split(":", 1)[1])  # STORAGE:GiB,mp=…
                have_gib = _size_gib(have)
                if have_gib is not None and have_gib + 1e-6 < want_gib:  # growing in the UI is fine
                    out.append(Diff(f"mount {path}", f"{want_gib:g}G", f"{have_gib:g}G (smaller)"))
    for path, v in live_mps.items():
        if path not in want_mps:
            out.append(Diff(f"mount {path}", "", v.split(",")[0] + " (only on CT)"))

    # raw lxc lines (multiset), minus what PVE derives from the image
    want_lxc = Counter(_lxc_line(x) for x in svc.strings("lxc_config_lines"))
    have_lxc = Counter(_lxc_line(x) for x in cur if x.startswith("lxc.") and _lxc_line(x)[0] not in IMAGE_DERIVED_LXC)
    for k, v in sorted((want_lxc - have_lxc).elements()):
        out.append(Diff(k, v or "(empty)", "missing"))
    for k, v in sorted((have_lxc - want_lxc).elements()):
        out.append(Diff(k, "", f"{v or '(empty)'} (only on CT)"))

    # devices
    want_dev = svc.strings("lxc_dev")
    have_dev = [cfg[k] for k in sorted((k for k in cfg if re.fullmatch(r"dev\d+", k)), key=lambda k: int(k[3:]))]
    if want_dev and want_dev != have_dev:
        out.append(Diff("lxc_dev", " | ".join(want_dev), " | ".join(have_dev)))

    # env: compose keys only, never values
    have_env: dict[str, str] = {}
    for line in cur:
        if line.startswith("env:"):
            for part in line[4:].lstrip().split("\0"):
                k, sep, v = part.partition("=")
                if sep and k:
                    have_env[k] = v
    for k, v in svc.env.items():
        if k not in have_env:
            out.append(Diff(f"env {k}", "set", "missing"))
        elif have_env[k] != v:
            out.append(Diff(f"env {k}", "set", "different value"))

    for k in sorted((k for k in cfg if re.fullmatch(r"unused\d+", k)), key=lambda k: int(k[6:])):
        out.append(Diff(k, "", f"{cfg[k]} (detached disk)"))
    return out
