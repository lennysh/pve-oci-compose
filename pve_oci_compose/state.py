"""Node-local state: which registry digest each vztmpl template was pulled at.

A template tarball's filename comes from the tag (``nginx_1.27.tar``), so after the tag
moves on the registry the old tarball would be silently reused. Recording the digest
lets pull() reuse a template only when it is still current.

File: ``$PVE_OCI_STATE_DIR/templates.json`` (default ``/var/lib/pve-oci-compose``).
Losing it is harmless — templates are just re-downloaded once.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def _path() -> Path:
    return Path(os.environ.get("PVE_OCI_STATE_DIR") or "/var/lib/pve-oci-compose") / "templates.json"


def _load() -> dict[str, str]:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def template_digest(ostemplate: str) -> str | None:
    return _load().get(ostemplate)


def record_template(ostemplate: str, digest: str | None) -> None:
    d = _load()
    if digest:
        d[ostemplate] = digest
    else:
        d.pop(ostemplate, None)
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".templates.", dir=str(p.parent))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1, sort_keys=True)
    os.replace(tmp, p)
