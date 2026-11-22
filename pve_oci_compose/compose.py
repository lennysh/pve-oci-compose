"""Compose file loading, defaults merge, validation and in-place vmid writeback."""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .errors import ComposeError
from .guestfs import GuestPathError, split_guest_path
from .inject import on_refresh

DEFAULT_MARKER = "/etc/pve-oci-compose.json"
SERVICE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")
LXC_LINE_RE = re.compile(r"^\s*[a-zA-Z][a-zA-Z0-9_.-]*\s*[:=]")
BIND_RE = re.compile(r"^/.+,mp=")
PRE_BACKUP_MODES = ("snapshot", "auto", "vzdump", "none")


def normalize_env(val) -> dict[str, str]:
    """env / environment as mapping, KEY=value list/string, or [{name:, value:}] → dict."""
    if val is None:
        return {}
    if isinstance(val, dict):
        return {str(k): "" if v is None else str(v) for k, v in val.items()}
    if isinstance(val, str):
        k, sep, v = val.partition("=")
        return {k: v} if sep and k else {}
    out: dict[str, str] = {}
    if isinstance(val, list):
        for item in val:
            if isinstance(item, str):
                k, sep, v = item.partition("=")
                if sep and k:
                    out[k] = v
            elif isinstance(item, dict):
                if item.get("name") is not None and "value" in item:
                    out[str(item["name"])] = str(item["value"])
                elif len(item) == 1:
                    k, v = next(iter(item.items()))
                    out[str(k)] = str(v)
    return out


def env_of(mapping: dict) -> dict[str, str]:
    out = normalize_env(mapping.get("environment"))
    out.update(normalize_env(mapping.get("env")))
    return out


def as_flag(v) -> str | None:
    """YAML bool / int / string → "1" / "0" / raw string for pct flags. None if unset."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


def as_scalar(v) -> str | None:
    if v is None or isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


@dataclass
class Service:
    name: str
    data: dict
    defaults_env: dict[str, str] = field(default_factory=dict)
    service_env: dict[str, str] = field(default_factory=dict)

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def image(self) -> str:
        v = self.data.get("image") or self.data.get("reference") or ""
        return str(v).strip()

    @property
    def vmid_spec(self) -> int | str:
        """int vmid, or "next" (next / auto / null / missing)."""
        v = self.data.get("vmid")
        if v is None or isinstance(v, bool):
            return "next"
        if isinstance(v, int):
            return v
        s = str(v).strip()
        if s.lower() in ("next", "auto", ""):
            return "next"
        if s.isdigit():
            return int(s)
        raise ComposeError(f"service {self.name}: vmid must be a number, next, auto, or null (got {s})")

    @property
    def vmid(self) -> int | None:
        spec = self.vmid_spec
        return spec if isinstance(spec, int) else None

    @property
    def template_storage(self) -> str | None:
        v = self.data.get("template_storage") or self.data.get("storage")
        return str(v) if v else None

    @property
    def env(self) -> dict[str, str]:
        return {**self.defaults_env, **self.service_env}

    def strings(self, key: str) -> list[str]:
        v = self.data.get(key)
        return [str(x) for x in v if isinstance(x, (str, int, float))] if isinstance(v, list) else []

    def inject_items(self) -> list[dict]:
        return list(self.data.get("rootfs_files") or []) + list(self.data.get("mount_files") or [])

    def pool(self, project: str | None) -> str | None:
        if "pool" in self.data:
            v = self.data["pool"]
            if v is None or v is False or v == "":
                return None
            return str(v)
        return project or None

    def as_inject_doc(self) -> dict:
        return self.data


@dataclass
class Compose:
    path: Path
    project: str | None
    about: str | None
    repo: str | bool | None
    services: list[Service]

    @property
    def dir(self) -> Path:
        return self.path.resolve().parent

    def find(self, name: str) -> Service | None:
        return next((s for s in self.services if s.name == name), None)


def load(path: str | os.PathLike) -> Compose:
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ComposeError(f"compose file not found or unreadable: {p} ({e.strerror})") from e
    try:
        doc = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise ComposeError(f"invalid YAML in {p}: {e}") from e
    doc = doc or {}
    if not isinstance(doc, dict):
        raise ComposeError("root of compose file must be a mapping")
    defaults = doc.get("defaults") or {}
    if not isinstance(defaults, dict):
        raise ComposeError("'defaults' must be a mapping")
    services = doc.get("services") or {}
    if not isinstance(services, dict):
        raise ComposeError("'services' must be a mapping")
    about = doc.get("about")
    if about is not None and not isinstance(about, str):
        raise ComposeError("top-level 'about' must be a string (multiline text) if set")
    repo = doc.get("repo")
    if repo is not None and not isinstance(repo, (str, bool)):
        raise ComposeError("top-level 'repo' must be a string (URL), false, or omitted")

    out: list[Service] = []
    denv = env_of(defaults)
    for name, svc in services.items():
        if svc is None:
            continue
        if not isinstance(svc, dict):
            raise ComposeError(f"service {name!r} must be a mapping")
        merged = {**defaults, **svc}
        merged.pop("environment", None)
        merged.pop("env", None)
        out.append(Service(str(name), merged, dict(denv), env_of(svc)))
    project = doc.get("name") or doc.get("project")
    return Compose(p, str(project) if project else None, about, repo, out)


# ---- validation -----------------------------------------------------------


def _check_list(svc: Service, key: str) -> None:
    v = svc.get(key)
    if v is not None and not isinstance(v, list):
        raise ComposeError(f"service {svc.name}: {key} must be a YAML list")


def valid_lxc_line(s: str) -> bool:
    if not s.strip() or s.lstrip().startswith(("[", "#")):
        return False
    return bool(LXC_LINE_RE.match(s))


def mount_guest_paths(svc: Service) -> set[str]:
    out: set[str] = set()
    for raw in svc.strings("mounts"):
        parts = raw.split(":")
        if len(parts) >= 3 and parts[2].split(",")[0].strip().startswith("/"):
            out.add(parts[2].split(",")[0].strip().rstrip("/") or "/")
    for raw in svc.strings("bind_mounts"):
        m = re.search(r",mp=([^,]+)", raw)
        if m and m.group(1).strip().startswith("/"):
            out.add(m.group(1).strip().rstrip("/") or "/")
    return out


def _validate_inject(svc: Service, compose_dir: Path) -> None:
    known = mount_guest_paths(svc)

    def err(msg):
        raise ComposeError(f"service {svc.name}: {msg}")

    for key in ("rootfs_files", "mount_files"):
        items = svc.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            err(f"{key} must be a YAML list")
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                err(f"{key}[{i}] must be a mapping")
            for k in ("on_refresh", "refresh"):
                if k in item and not isinstance(item[k], (bool, int, float, str)):
                    err(f"{key}[{i}].{k} must be boolean")
            on_refresh(item)
            m = item.get("mode")
            if m is not None and not (
                (isinstance(m, int) and not isinstance(m, bool) and 0 <= m <= 0o7777)
                or (isinstance(m, str) and re.fullmatch(r"0?[0-7]{1,4}", m.strip()))
            ):
                err(f"{key}[{i}] mode must be octal string or integer (got {m!r})")
            for ok in ("owner", "group"):
                v = item.get(ok)
                if v is not None and not (
                    (isinstance(v, int) and not isinstance(v, bool) and v >= 0)
                    or (isinstance(v, str) and v.isdigit())
                ):
                    err(f"{key}[{i}].{ok} must be a non-negative integer")
            hs, hc = bool(item.get("source")), "content" in item
            if hs and hc:
                err(f"{key}[{i}]: use source or content, not both")
            if not hs and not hc:
                err(f"{key}[{i}]: missing source or content")
            if hs:
                src = str(item["source"]).strip()
                sp = Path(src) if src.startswith("/") else compose_dir / src
                if not sp.is_file():
                    err(f"{key}[{i}]: source not a readable file: {sp}")
            path = item.get("path")
            if not isinstance(path, str) or not path.strip():
                err(f"{key}[{i}].path is required")
            if key == "rootfs_files" and not path.startswith("/"):
                err(f"rootfs_files[{i}].path must be an absolute guest path (e.g. /etc/resolv.conf)")
            try:
                split_guest_path(path)
            except GuestPathError as e:
                err(f"{key}[{i}].path: {e}")
            if key == "mount_files":
                mp = item.get("mount") or item.get("mp")
                if not isinstance(mp, str) or not mp.strip():
                    err(f"mount_files[{i}] needs mount: (guest mp= path from mounts or bind_mounts)")
                if ".." in mp.split("/"):
                    err(f"mount_files[{i}].mount: '..' is not allowed")
                mpn = mp.rstrip("/") or "/"
                if not known:
                    err("mount_files requires at least one mounts: or bind_mounts: entry with a guest mp= path")
                if mpn not in known:
                    err(f"mount_files[{i}].mount {mp!r} not found in mounts/bind_mounts (known: {', '.join(sorted(known))})")
                if path.startswith("/") and not (path == mpn or path.startswith(mpn + "/")):
                    err(f"mount_files[{i}].path {path!r} is not under mount {mp!r}")


def validate(svc: Service, compose_dir: Path, need_rootfs: bool = True) -> None:
    svc.vmid_spec  # raises on bad vmid
    if not svc.image:
        raise ComposeError(f"service {svc.name}: missing image (or reference)")
    if need_rootfs and not svc.get("rootfs"):
        raise ComposeError(f"service {svc.name}: missing rootfs")
    sk = svc.get("ssh_public_keys")
    if isinstance(sk, str) and sk and not os.path.isfile(sk):
        raise ComposeError(f"service {svc.name}: ssh_public_keys must be a readable host file (got: {sk})")
    for key in ("guest_ports", "bind_mounts", "lxc_config_lines", "mounts", "lxc_dev", "unused_disks"):
        _check_list(svc, key)
    if svc.get("about") is not None and not isinstance(svc.get("about"), str):
        raise ComposeError(f"service {svc.name}: about must be a string if set")
    for bm in svc.strings("bind_mounts"):
        if not BIND_RE.match(bm):
            raise ComposeError(
                f"service {svc.name}: each bind_mounts entry must start with '/' and contain ',mp=': {bm[:120]}"
            )
    for lx in svc.strings("lxc_config_lines"):
        if not valid_lxc_line(lx):
            raise ComposeError(f"service {svc.name}: invalid lxc_config_lines entry (KEY: value or KEY = value): {lx[:120]}")
    for mp in svc.strings("mounts"):
        parse_mount_spec(mp, svc.name)
    pb = svc.get("refresh_pre_backup")
    if pb is not None and pb not in PRE_BACKUP_MODES:
        raise ComposeError(f"service {svc.name}: refresh_pre_backup must be one of {', '.join(PRE_BACKUP_MODES)}")
    _validate_inject(svc, compose_dir)


def parse_mount_spec(spec: str, svc_name: str = "?") -> str:
    """``STORAGE:GiB:/path[:opts]`` → pct mpN value (``backup=1`` unless set)."""
    m = re.match(r"^([^:]+):([0-9]+(?:\.[0-9]+)?):(.*)$", spec)
    if not m:
        raise ComposeError(f"service {svc_name}: invalid mounts entry {spec!r} (expected STORAGE:GiB:/path[:opts])")
    st, size, rest = m.group(1), m.group(2), m.group(3)
    if float(size) <= 0:
        raise ComposeError(f"service {svc_name}: mounts entry {spec!r}: size must be > 0")
    path, _, extra = rest.partition(":")
    if not path.startswith("/"):
        raise ComposeError(f"service {svc_name}: mounts entry {spec!r}: path must be absolute")
    val = f"{st}:{size},mp={path}"
    if extra:
        val += f",{extra}"
    if "backup=" not in val:
        val += ",backup=1"
    return val


# ---- vmid writeback -----------------------------------------------------------


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def rewrite_vmid(text: str, svc: str, vmid: int) -> str:
    """Replace the first ``vmid:`` line under ``services.<svc>``; keeps comments/layout."""
    if not SERVICE_KEY_RE.match(svc):
        raise ComposeError(
            f"service '{svc}': vmid writeback only supports names matching ^[a-zA-Z0-9_-]+$"
        )
    lines = text.splitlines(keepends=True)
    in_services = found = False
    sind = -1
    key_re = re.compile(rf"^\s*{re.escape(svc)}:\s*(#.*)?$")
    for i, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        if not in_services:
            if re.match(r"^services:\s*(#.*)?$", line):
                in_services = True
            continue
        if not found:
            if line.strip() and not line.lstrip().startswith("#") and _indent(line) == 0:
                break  # left the services: block
            if key_re.match(line):
                found, sind = True, _indent(line)
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if _indent(line) <= sind:
            break
        m = re.match(r"^(\s+vmid\s*:)\s*([^#]*?)\s*(#.*)?$", line)
        if m:
            nl = raw[len(line):]
            new = f"{m.group(1)} {vmid}" + (f" {m.group(3)}" if m.group(3) else "")
            lines[i] = new + nl
            return "".join(lines)
    if not found:
        raise ComposeError(f"no services.{svc} in compose (expected a mapping key under services:)")
    raise ComposeError(f'services["{svc}"] has no vmid: line to update; add e.g.  vmid: next')


def write_vmid(path: Path, svc: str, vmid: int) -> None:
    text = path.read_text(encoding="utf-8")
    new = rewrite_vmid(text, svc, vmid)
    fd, tmp = tempfile.mkstemp(prefix=".pve-oci-compose.", dir=str(path.resolve().parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new)
        try:
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
