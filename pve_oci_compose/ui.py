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


def say(msg: str = "") -> None:
    print(msg, flush=True)


def title(msg: str) -> None:
    print(f"\n{B}{M}{msg}{R}\n{D}{'─' * _width()}{R}", flush=True)


def sub(msg: str) -> None:
    print(f"\n{B}{msg}{R}", flush=True)


def kv(key: str, value: object) -> None:
    print(f"  {key:<20}  {value}", flush=True)


def muted(msg: str) -> None:
    print(f"  {D}{msg}{R}", flush=True)


def detail(msg: str) -> None:
    if verbose():
        print(f"  {D}{msg}{R}", flush=True)


def ok(msg: str) -> None:
    print(f"{G}✓ {msg}{R}", flush=True)


def warn(msg: str) -> None:
    print(f"{Y}! {msg}{R}", file=sys.stderr, flush=True)


def err(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


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
        print(f"  {D}{redact(argv)}{R}", flush=True)
