"""Command line: ``python3 -m pve_oci_compose [options] {plan,apply,refresh,pull}``."""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import sys

from . import __version__, commands, ui
from . import compose as C
from .errors import ComposeError, Terminated
from .pve import Pve

EPILOG = """\
vmid: a number, or next / auto / null / omitted to allocate the cluster's next free id
at apply (written back into the compose file unless --no-write-compose).

Env: PVE_OCI_VERBOSE=1 (show commands), PVE_OCI_ROOTFS_MARKER, PVE_OCI_POOL_NO_AUTOCREATE=1,
OCI_CT_CREATE_NET0, OCI_CT_CREATE_NO_RESOLVE_LATEST=1, OCI_REFRESH_TEMP_NET0,
OCI_REFRESH_SHUTDOWN_TIMEOUT, OCI_REFRESH_NO_AUTO_ROLLBACK=1. See README.md for the schema.
"""


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pve-oci-compose",
        description="Declarative OCI LXC containers on Proxmox VE from a compose YAML file.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("command", choices=("plan", "apply", "refresh", "pull"))
    ap.add_argument("-f", "--file", default=os.environ.get("COMPOSE_FILE") or "compose.yaml",
                    help="compose file (default ./compose.yaml or $COMPOSE_FILE)")
    ap.add_argument("-n", "--dry-run", action="store_true", help="print state-changing commands only")
    ap.add_argument("--service", help="only this services: key")
    ap.add_argument("--vmid", type=int, help="only the service with this numeric vmid")
    ap.add_argument("--no-write-compose", action="store_true",
                    help="apply: do not write an allocated vmid back into the compose file")
    g = ap.add_argument_group("refresh")
    g.add_argument("--adopt", action="store_true", help="refresh a CT that has no guest marker yet")
    g.add_argument("--force", action="store_true", help="refresh even when the stored ref matches")
    g.add_argument("--pre-backup", choices=C.PRE_BACKUP_MODES, help="default: compose refresh_pre_backup or snapshot")
    g.add_argument("--no-snapshot", action="store_true", help="same as --pre-backup none")
    g.add_argument("--backup-storage", help="vzdump storage for --pre-backup auto/vzdump")
    g.add_argument("--allow-failed-snapshot", action="store_true", help="continue if the pre-backup fails")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return ap


def _check_args(a: argparse.Namespace) -> None:
    if a.no_snapshot:
        if a.pre_backup and a.pre_backup != "none":
            raise ComposeError(f"cannot combine --no-snapshot with --pre-backup {a.pre_backup}")
        a.pre_backup = "none"
    if a.pre_backup == "none" and a.allow_failed_snapshot:
        raise ComposeError("cannot combine --pre-backup none / --no-snapshot with --allow-failed-snapshot")
    if a.command != "refresh":
        if a.adopt or a.force or a.pre_backup or a.backup_storage or a.allow_failed_snapshot:
            raise ComposeError("--adopt, --force, --pre-backup, --no-snapshot, --backup-storage and "
                               "--allow-failed-snapshot are only valid with refresh")


def _preflight(need: list[str]) -> None:
    if os.geteuid() != 0:
        raise ComposeError("run as root on a Proxmox VE node")
    missing = [t for t in need if not shutil.which(t)]
    if missing:
        raise ComposeError(f"missing tools: {', '.join(missing)} (see bindep.txt)")


def _raise_terminated(signum, _frame):
    raise Terminated(signum)


def install_signal_handlers() -> None:
    """Turn SIGTERM / SIGHUP into an exception so an interrupted refresh/apply still runs
    its cleanup (remove temp CT, roll back, restart). Without this, an SSH drop (SIGHUP)
    kills the process mid-refresh with nothing cleaned up."""
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_terminated)


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    install_signal_handlers()
    try:
        _check_args(a)
        if a.vmid is not None and a.vmid < 0:
            raise ComposeError("--vmid must be a positive number")
        need = ["pct", "pvesh", "perl", "skopeo"] + (["rsync"] if a.command == "refresh" else [])
        _preflight(need)
        c = C.load(a.file)
        if os.environ.get("PVE_OCI_COMPOSE_NO_WRITE") == "1":
            a.no_write_compose = True
        o = commands.Options(
            service=a.service, vmid=a.vmid, write_compose=not a.no_write_compose, adopt=a.adopt,
            force=a.force, pre_backup=a.pre_backup, backup_storage=a.backup_storage,
            allow_failed_snapshot=a.allow_failed_snapshot,
        )
        pve = Pve(dry_run=a.dry_run)
        {"plan": commands.plan, "apply": commands.apply, "refresh": commands.refresh,
         "pull": commands.pull_only}[a.command](pve, c, o)
        return 0
    except ComposeError as e:
        ui.err(f"pve-oci-compose: {e}")
        return 1
    except KeyboardInterrupt:
        ui.err("pve-oci-compose: interrupted")
        return 130
    except Terminated as e:
        ui.err(f"pve-oci-compose: {e}")
        return 128 + e.signum


if __name__ == "__main__":
    sys.exit(main())
