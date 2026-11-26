"""Human-readable version for an image digest — display only, never used to pick the image.

What runs is decided by the digest alone (see create.resolve_template). This module only
answers "which version is that?", and only from sources that cannot be wrong:

1. The compose tag itself when it already looks like a version (``1.2.3``, ``v2.0``).
2. Registries that list tags *with digests* (Docker Hub): every tag sharing the digest.
3. The publisher's ``org.opencontainers.image.version`` label, if it looks like a version.
4. Digest-verified probes of the most likely tags (newest plain releases first; every tag
   for small repos). A match is proof; no match means "unknown" — never a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import refs
from .pve import host_arch

VERSIONISH = re.compile(r"^v?\d+(\.\d+)+([-+_.][0-9A-Za-z.]+)?$")
MAX_PROBES = 5  # registry manifest GETs for big repos (rate limits)
SMALL_REPO = 30  # probe every tag when the repo has at most this many


@dataclass
class VersionInfo:
    version: str | None = None
    aliases: list[str] = field(default_factory=list)  # other tags with the same digest (may be partial)
    source: str = ""  # where the version came from, for the logs

    def label(self) -> str | None:
        return self.version


def best_version(tags: list[str]) -> str | None:
    """Most specific version-looking tag (3.24.4 over 3.24 over 3), else None."""
    cands = [t for t in refs.tag_candidates(tags) if VERSIONISH.match(t)]
    return cands[0] if cands else None


def _hub_match(t: dict, digest: str, index_digest: str, arch: str) -> bool:
    if index_digest and t.get("digest") == index_digest:
        return True
    return any(isinstance(i, dict) and i.get("digest") == digest and i.get("architecture") == arch
               and (i.get("os") in (None, "linux")) for i in t.get("images") or [])


def _docker_hub(pve, repo: str, digest: str, index_digest: str = "") -> list[str] | None:
    """Tags whose image for this node's platform is ``digest``, via the Docker Hub API
    (tag list with per-architecture digests, one call per 100 tags)."""
    if not repo.startswith("docker.io/"):
        return None
    path = repo[len("docker.io/"):]
    ns, _, name = path.partition("/") if "/" in path else ("library", "", path)
    if not name:
        return None
    url = f"https://hub.docker.com/v2/namespaces/{ns}/repositories/{name}/tags?page_size=100"
    found: list[str] = []
    for _ in range(3):  # newest first; the current tags are on the first page(s)
        doc = pve.http_json(url)
        if not isinstance(doc, dict):
            return found or None
        for t in doc.get("results") or []:
            if isinstance(t, dict) and _hub_match(t, digest, index_digest, host_arch()):
                found.append(str(t.get("name")))
        url = doc.get("next")
        if found or not url:
            break
    return found


def identify(pve, ref: str, digest: str, index_digest: str = "", inspect: dict | None = None) -> VersionInfo:
    """Version of the image ``ref`` currently resolves to. ``digest`` is the image for this
    node's platform; a candidate tag matches when it resolves to that same image."""
    index_digest = index_digest or digest  # single-arch image: the tag points straight at it
    repo, tag, _ = refs.split_ref(ref)
    if tag and VERSIONISH.match(tag):
        return VersionInfo(tag, [], "compose tag")

    hub = _docker_hub(pve, repo, digest, index_digest)
    if hub:
        others = [t for t in hub if t != tag]
        v = best_version(hub)
        if v:
            return VersionInfo(v, others, "Docker Hub tag list")
        if others:
            return VersionInfo(None, others, "Docker Hub tag list")

    info = inspect if inspect is not None else (pve.skopeo_inspect(ref) or {})
    labels = info.get("Labels") or {}
    lab = labels.get("org.opencontainers.image.version") or ""
    if VERSIONISH.match(str(lab)):
        return VersionInfo(str(lab), [], "image label")

    all_tags = [t for t in (info.get("RepoTags") or []) if t != tag]
    if not all_tags:
        return VersionInfo()
    cands = refs.tag_candidates(all_tags)
    small = len(all_tags) <= SMALL_REPO
    if not small:
        cands = [t for t in cands if VERSIONISH.match(t)][:MAX_PROBES]
    matches = []
    for t in cands:
        res = pve.registry_resolve(f"{repo}:{t}")
        if res is not None and res.digest == digest:
            matches.append(t)
            if not small:  # big repo: the first verified (= most specific) release is enough
                break
    if matches:
        return VersionInfo(best_version(matches) or matches[0], matches, "verified tag")
    return VersionInfo()
