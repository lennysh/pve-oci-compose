"""Markdown text for ``pct --description``."""

from __future__ import annotations

from urllib.parse import urlparse

from .compose import Compose, Service

DEFAULT_REPO = "https://github.com/lennysh/pve-oci-compose"


def _s(x) -> str:
    return x.strip() if isinstance(x, str) else ""


def _footer_url(cfg) -> str | None:
    if cfg is False:
        return None
    if isinstance(cfg, str) and cfg.strip():
        return cfg.strip()
    return DEFAULT_REPO


def _label(url: str) -> str:
    if url == DEFAULT_REPO:
        return "lennysh/pve-oci-compose"
    parts = [x for x in urlparse(url).path.split("/") if x]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return parts[-1] if parts else "repository"


def _ports(gp) -> list[str]:
    out = []
    for item in gp if isinstance(gp, list) else []:
        if isinstance(item, str) and item.strip():
            out.append(f"- {item.strip()}")
        elif isinstance(item, dict):
            p = item.get("port", item.get("port_number"))
            if p is None:
                continue
            pr = str(item.get("proto") or item.get("protocol") or "tcp").strip().lower() or "tcp"
            d = _s(item.get("description") or item.get("desc"))
            out.append(f"- **{p}/{pr}** — {d}" if d else f"- **{p}/{pr}**")
    return out


def build(
    compose: Compose, svc: Service, pulled_ref: str | None = None, synced_at: str | None = None
) -> str | None:
    body_user = _s(svc.get("description"))
    img = svc.image
    about = "\n\n".join(x for x in (_s(compose.about), _s(svc.get("about"))) if x)
    ports = _ports(svc.get("guest_ports"))
    rich = bool(ports or about)
    if not body_user and not rich and not img:
        return None
    if body_user and not rich:
        return body_user.rstrip() + "\n"
    lines = [
        "# pve-oci-compose",
        "",
        f"- **Stack:** {compose.project}" if compose.project else "- **Stack:** _(compose file)_",
        f"- **Service:** {svc.name}",
    ]
    if pulled_ref:
        lines.append(f"- **Image (pulled):** `{pulled_ref}`")
        if img and img != pulled_ref:
            lines.append(f"- **Compose file ref:** `{img}`")
    elif img:
        lines.append(f"- **Image:** `{img}`")
    if synced_at:
        lines.append(f"- **Template sync:** {synced_at}")
    if body_user:
        lines += ["", *body_user.splitlines()]
    if ports:
        lines += [
            "",
            "## Listener ports (inside the CT)",
            "",
            "These are sockets that listen **in the guest** (not Docker Compose `ports:` publish maps). "
            "Reach them via the CT IP, host firewall, or a reverse proxy.",
            "",
            *ports,
        ]
    if about:
        lines += ["", "## Notes", "", about]
    url = _footer_url(compose.repo)
    if url:
        lines += ["", "---", "", f"- **Source:** [{_label(url)}]({url})"]
    return "\n".join(lines).rstrip() + "\n"


def needs_runtime_update(compose: Compose, svc: Service) -> bool:
    """True when the description shows the pulled ref / sync time (not a verbatim user text)."""
    rich = bool(svc.get("guest_ports")) or bool(_s(svc.get("about"))) or bool(_s(compose.about))
    user_only = bool(_s(svc.get("description"))) and not rich
    return rich or (bool(svc.image) and not user_only)
