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


def pick_tag(tags: list[str]) -> str | None:
    """Best concrete tag for a digest: skip latest, cosign/notation .sig and sha256-* tags."""
    cands = [t for t in tags if t.lower() != "latest" and not t.endswith(".sig") and not t.startswith("sha256-")]
    if not cands:
        return None

    def key(t: str):
        return [(0, int(x), "") if x.isdigit() else (1, 0, x) for x in re.split(r"(\d+)", t) if x]

    return sorted(cands, key=key)[-1]


def resolve_floating(pve, ref: str) -> str:
    """Pin a floating :latest ref to a same-digest concrete tag, else name@digest."""
    info = pve.skopeo_inspect(ref)
    if not info or not info.get("Name") or not info.get("Digest"):
        raise ComposeError(f"skopeo inspect failed for docker://{ref} (or set OCI_CT_CREATE_NO_RESOLVE_LATEST=1)")
    name, digest = info["Name"], info["Digest"]
    at = pve.skopeo_inspect(f"{name}@{digest}") or {}
    tag = pick_tag(list(at.get("RepoTags") or []))
    if tag:
        ui.muted(f"Resolved floating tag to same-digest ref: {name}:{tag}")
        return f"{name}:{tag}"
    ui.muted("No non-latest tag for this manifest; using digest ref")
    return f"{name}@{digest}"
