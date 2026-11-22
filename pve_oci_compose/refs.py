"""OCI image reference handling."""

from __future__ import annotations

import re

from . import ui
from .errors import ComposeError


def strip_scheme(ref: str) -> str:
    for p in ("oci://", "docker://"):
        if ref.startswith(p):
            return ref[len(p) :]
    return ref


def is_registry_ref(ref: str) -> bool:
    """False for local templates (``local:vztmpl/x.tar``) and paths / URLs."""
    return not (ref.startswith(("/", "./", "../", "http://", "https://")) or ":vztmpl/" in ref or ":import/" in ref)


def normalize(ref: str) -> str:
    """Docker Hub short refs → fully qualified (``nginx:1`` → ``docker.io/library/nginx:1``).

    Like Docker: the first path component is a registry host when it contains ``.`` or
    ``:`` (port) or is ``localhost``.
    """
    ref = strip_scheme(ref.strip())
    if not ref:
        raise ComposeError("empty image reference")
    parts = ref.split("/")
    if len(parts) > 1:
        first = parts[0]
        if "." in first or ":" in first or first == "localhost":
            return ref
        return f"docker.io/{ref}"
    return f"docker.io/library/{ref}"


def is_floating_latest(ref: str, normalized_filename: str) -> bool:
    return ref.lower().endswith(":latest") or normalized_filename.endswith("_latest")


_PLAIN_VERSION = re.compile(r"^v?\d+(\.\d+)*$")
MAX_TAG_PROBES = 5  # each probe is one registry manifest GET (Docker Hub rate limits)


def _version_key(t: str):
    return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"(\d+)", t) if x]


def tag_candidates(tags: list[str]) -> list[str]:
    """Tags worth probing for a floating ref, best first: dotted versions (1.27.3, v2.1), then
    bare numbers (e.g. date snapshots), then anything else — each by version descending.
    Skips latest, cosign/notation .sig and sha256-*."""
    cands = [t for t in tags if t.lower() != "latest" and not t.endswith(".sig") and not t.startswith("sha256-")]

    def tier(t: str) -> int:
        if _PLAIN_VERSION.match(t):
            return 0 if "." in t else 1  # 3.22.2 / v1.2 before bare numbers (often date snapshots)
        return 2

    def key(t: str):
        v = t[1:] if tier(t) < 2 and t[:1] in "vV" else t
        # trailing (2, …) sorts after any further component, so 3.24.1 comes before 3.24
        return (tier(t), [_neg(k) for k in _version_key(v)] + [(2, 0, "")])

    return sorted(cands, key=key)


def _neg(k):
    # sort helper: descending version order inside a tier
    kind, num, txt = k
    return (kind, -num, "".join(chr(0x10FFFF - ord(c)) for c in txt))


def resolve_floating(pve, ref: str, digest: str | None) -> str:
    """Pin a floating ref (``:latest``) to a concrete tag **with the same digest**, else
    ``name@digest``. ``RepoTags`` lists every tag of the repository, so candidates are
    verified by digest (a few probes, most likely first) rather than trusted."""
    info = pve.skopeo_inspect(ref)
    if not info or not info.get("Name"):
        raise ComposeError(f"skopeo inspect failed for docker://{ref} (or set OCI_CT_CREATE_NO_RESOLVE_LATEST=1)")
    name = info["Name"]
    digest = digest or info.get("Digest")
    if not digest:
        raise ComposeError(f"could not read the digest of docker://{ref}")
    for tag in tag_candidates(list(info.get("RepoTags") or []))[:MAX_TAG_PROBES]:
        if pve.registry_digest(f"{name}:{tag}") == digest:
            return f"{name}:{tag}"
    ui.muted(f"no concrete tag found for this digest; using {name}@{digest[:19]}…")
    return f"{name}@{digest}"
