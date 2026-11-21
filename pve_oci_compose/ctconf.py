"""Edits to ``/etc/pve/lxc/<vmid>.conf`` that pct has no flag for (env merge, raw lxc.* lines).

Only the current-config section (before the first ``[snapshot]``) is touched.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

CONF_DIR = Path("/etc/pve/lxc")


def _split_sections(text: str) -> tuple[list[str], str]:
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.startswith("["):
            return lines[:i], "".join(lines[i:])
    return lines, ""


def _join(body: list[str], extra: list[str], rest: str) -> str:
    while body and not body[-1].strip():
        body.pop()
    out = "".join(body)
    if out and not out.endswith("\n"):
        out += "\n"
    out += "".join(x if x.endswith("\n") else x + "\n" for x in extra)
    if rest:
        out += "\n" + rest
    return out


def merge_env_text(text: str, layers: list[dict[str, str]], keep_existing: bool = True) -> str | None:
    """Existing env (``env:`` / ``lxc.environment.runtime:``, i.e. the image env right after
    create) overlaid by ``layers``. ``keep_existing=False`` replaces it entirely.

    Returns new config text, or None when there is nothing to change.
    """
    if keep_existing and not any(layers):
        return None
    body, rest = _split_sections(text)
    merged: OrderedDict[str, str] = OrderedDict()
    kept = []
    for line in body:
        raw = line.rstrip("\n")
        if raw.startswith("env:"):
            if keep_existing:
                merged.update(parse_env_line(raw))
            continue
        if raw.startswith("lxc.environment.runtime:"):
            if keep_existing:
                merged.update(parse_env_line(raw))
            continue
        kept.append(line)
    for layer in layers:
        for k, v in layer.items():
            if "\n" in k or "\n" in v or "\0" in k or "\0" in v or "=" in k:
                raise ValueError(f"invalid env entry {k!r} (no newlines / NUL; no '=' in key)")
            merged[str(k)] = str(v)
    if not merged:
        return _join(kept, [], rest)
    line = "env: " + "\0".join(f"{k}={v}" for k, v in merged.items())
    return _join(kept, [line], rest)


def parse_env_line(raw: str) -> dict[str, str]:
    """One ``env:`` (NUL-separated) or ``lxc.environment.runtime:`` line → dict."""
    out: dict[str, str] = {}
    if raw.startswith("env:"):
        for part in raw[4:].lstrip().split("\0"):
            k, sep, v = part.strip().partition("=")
            if sep and k:
                out[k] = v
    elif raw.startswith("lxc.environment.runtime:"):
        k, sep, v = raw.partition(":")[2].strip().partition("=")
        if sep and k:
            out[k] = v
    return out


def env_from_text(text: str) -> dict[str, str]:
    body, _ = _split_sections(text)
    out: dict[str, str] = {}
    for line in body:
        out.update(parse_env_line(line.rstrip("\n")))
    return out


def append_lines_text(text: str, new_lines: list[str]) -> str:
    body, rest = _split_sections(text)
    # drop legacy comment-marked blocks from older versions
    out, skip = [], False
    for line in body:
        s = line.rstrip("\n")
        if s == "# BEGIN pve-oci-compose lxc_config_lines":
            skip = True
            continue
        if s == "# END pve-oci-compose lxc_config_lines":
            skip = False
            continue
        if not skip:
            out.append(line)
    return _join(out, new_lines, rest)


def _rewrite(vmid: int, fn) -> None:
    cfg = CONF_DIR / f"{vmid}.conf"
    text = cfg.read_text(encoding="utf-8", errors="surrogateescape")
    new = fn(text)
    if new is not None and new != text:
        # pmxcfs: write in place (rename/chmod semantics are restricted there)
        cfg.write_text(new, encoding="utf-8", errors="surrogateescape")


def merge_env(vmid: int, layers: list[dict[str, str]], keep_existing: bool = True) -> None:
    _rewrite(vmid, lambda t: merge_env_text(t, layers, keep_existing))


def read_env(vmid: int) -> dict[str, str]:
    return env_from_text((CONF_DIR / f"{vmid}.conf").read_text(encoding="utf-8", errors="surrogateescape"))


def append_lines(vmid: int, lines: list[str]) -> None:
    if lines:
        _rewrite(vmid, lambda t: append_lines_text(t, lines))
