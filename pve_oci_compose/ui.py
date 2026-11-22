"""Terminal output.

NO_COLOR=1 or a non-tty disables colour. PVE_OCI_VERBOSE=1 enables detail() and cmd().
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys

_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()
B = "\033[1m" if _COLOR else ""
D = "\033[2m" if _COLOR else ""
G = "\033[32m" if _COLOR else ""
Y = "\033[33m" if _COLOR else ""
M = "\033[36m" if _COLOR else ""
R = "\033[0m" if _COLOR else ""


def verbose() -> bool:
    return os.environ.get("PVE_OCI_VERBOSE") == "1"


def _width() -> int:
    return max(48, min(100, shutil.get_terminal_size((80, 24)).columns - 4))


def _out(msg: str, stream=None) -> None:
    """print() that survives a vanished terminal (EIO / EPIPE after an SSH drop):
    losing a log line must never abort cleanup."""
    try:
        print(msg, file=stream or sys.stdout, flush=True)
    except (OSError, ValueError):
        pass


def say(msg: str = "") -> None:
    _out(msg)


def title(msg: str) -> None:
    _out(f"\n{B}{M}{msg}{R}\n{D}{'─' * _width()}{R}")


def sub(msg: str) -> None:
    _out(f"\n{B}{msg}{R}")


def kv(key: str, value: object) -> None:
    _out(f"  {key:<20}  {value}")


def muted(msg: str) -> None:
    _out(f"  {D}{msg}{R}")


def detail(msg: str) -> None:
    if verbose():
        _out(f"  {D}{msg}{R}")


def ok(msg: str) -> None:
    _out(f"{G}✓ {msg}{R}")


def warn(msg: str) -> None:
    _out(f"{Y}! {msg}{R}", sys.stderr)


def err(msg: str) -> None:
    _out(msg, sys.stderr)


SECRET_FLAGS = frozenset({"--password"})


def redact(cmd: list[str]) -> str:
    """Shell-quoted command line with secret flag values replaced by ***."""
    out: list[str] = []
    hide = False
    for a in cmd:
        if hide:
            out.append("'***'")
            hide = False
            continue
        out.append(shlex.quote(a))
        hide = a in SECRET_FLAGS
    return " ".join(out)


def cmd(argv: list[str]) -> None:
    if verbose():
        _out(f"  {D}{redact(argv)}{R}")
