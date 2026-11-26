"""Terminal output: plain helpers + a step timeline for apply / refresh / pull.

    ━━ app · CT 228 · refresh ━━━━━━━━━━━━━━━━━━━━━━━━━━━
       ghcr.io/antimatterchat/antimatter
       sha-b723ce8 → sha-2179a47   (image ref changed)

     [1/5] Pull image + build temp CT 142    ✓  1m 12s
     [2/5] Shut down CT 228                  ✓      4s
           ── downtime starts ──
     …
     ✓ Refreshed in 3m 59s · downtime 2m 48s

On an interactive terminal the running step shows a spinner and a live timer. Output
produced *during* a step is collected and printed under the step's result line, so the
layout is identical on a tty and in logs / cron. Warnings and errors print immediately.

NO_COLOR=1 or a non-tty disables colour. PVE_OCI_VERBOSE=1 disables the spinner, prints
everything immediately and shows the full commands (child process output is streamed).
"""

from __future__ import annotations

import os
import shlex
import shutil
import sys
import threading
import time
from contextlib import contextmanager

_COLOR = not os.environ.get("NO_COLOR") and sys.stdout.isatty()
B = "\033[1m" if _COLOR else ""
D = "\033[2m" if _COLOR else ""
G = "\033[32m" if _COLOR else ""
Y = "\033[33m" if _COLOR else ""
RED = "\033[31m" if _COLOR else ""
M = "\033[36m" if _COLOR else ""
R = "\033[0m" if _COLOR else ""

LABEL_W = 38
_LOCK = threading.RLock()
_step: "_Step | None" = None  # the step currently running (collects output)


def verbose() -> bool:
    return os.environ.get("PVE_OCI_VERBOSE") == "1"


def _width() -> int:
    return max(48, min(72, shutil.get_terminal_size((80, 24)).columns - 4))


def fmt_duration(sec: float) -> str:
    s = int(round(sec))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {s % 3600 // 60:02d}m"


def _write(msg: str, stream) -> None:
    """Write a line, survive a vanished terminal (EIO / EPIPE after an SSH drop), and keep
    a live spinner line intact."""
    with _LOCK:
        try:
            spin = _step.spinner if _step else None
            if spin:
                spin.clear()
            print(msg, file=stream, flush=True)
            if spin:
                spin.draw()
        except (OSError, ValueError):
            pass


def _out(msg: str, stream=None) -> None:
    stream = stream or sys.stdout
    if _step is not None and stream is sys.stdout and not verbose():
        _step.notes.append(msg)  # shown under the step's result line
        return
    _write(msg, stream)


def say(msg: str = "") -> None:
    _out(msg)


def title(msg: str) -> None:
    _out(f"\n{B}{M}{msg}{R}\n{D}{'─' * _width()}{R}")


def sub(msg: str) -> None:
    _out(f"\n{B}{msg}{R}")


def kv(key: str, value: object) -> None:
    _out(f"  {key:<20}  {value}")


def muted(msg: str) -> None:
    _out(f"       {D}{msg}{R}" if _step else f"  {D}{msg}{R}")


def detail(msg: str) -> None:
    if verbose():
        _write(f"  {D}{msg}{R}", sys.stdout)


def ok(msg: str) -> None:
    _out(f"{G}✓ {msg}{R}")


def warn(msg: str) -> None:
    _write(f"{Y}! {msg}{R}", sys.stderr)


def err(msg: str) -> None:
    _write(msg, sys.stderr)


def skip(name: str, what: str, reason: str) -> None:
    """One-line 'nothing to do' result for a service."""
    _write(f"{D} · {name} · {what} — {reason}{R}", sys.stdout)


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
        _write(f"  {D}{redact(argv)}{R}", sys.stdout)


# ---- step timeline -----------------------------------------------------------------------


def split_image(ref: str) -> tuple[str, str]:
    """``ghcr.io/o/app:1.2`` → (``ghcr.io/o/app``, ``1.2``); digests keep ``@sha256:…``."""
    if "@" in ref:
        repo, _, dig = ref.partition("@")
        return repo, "@" + dig
    head, _, last = ref.rpartition("/")
    if ":" in last:
        name, _, tag = last.rpartition(":")
        return (f"{head}/{name}" if head else name), tag
    return ref, ""


def short_digest(d: str | None) -> str:
    return d.split(":", 1)[-1][:12] if d else "?"


def _ident(tag: str, version: str | None, digest: str | None) -> str:
    """How to name one side of a change: version if known (and not just the tag), else digest."""
    if version and version != tag:
        return version
    return short_digest(digest) if (digest and not version) else (version or tag or short_digest(digest))


def image_lines(old: str | None, new: str, old_version=None, new_version=None,
                old_digest=None, new_digest=None) -> list[str]:
    """Header lines for an image (create) or an image change (refresh).

    same ref, moved tag:   ghcr.io/x/frigate · stable: 0.17.1 → 0.18.3
    tag changed:           ghcr.io/x/app · sha-b723ce8 → sha-2179a47
    create:                docker.io/library/nginx:latest = 1.31.9
    """
    nrepo, ntag = split_image(new)
    if not old:
        v = f" = {B}{new_version}{R}" if new_version and new_version != ntag else ""
        d = f"  {D}{short_digest(new_digest)}{R}" if new_digest and not new_version else ""
        return [f"   {new}{v}{d}"]
    orepo, otag = split_image(old)
    if orepo != nrepo:
        return [f"   {old}", f"   → {B}{new}{R}"]
    if otag == ntag:  # floating tag moved (or --force)
        left = _ident(otag, old_version, old_digest) if (old_version or old_digest) else "previous"
        right = _ident(ntag, new_version, new_digest)
        if old_digest and new_digest and old_digest == new_digest:
            return [f"   {nrepo}", f"   {ntag}: {right} {D}(unchanged){R}"]
        return [f"   {nrepo}", f"   {ntag}: {left} → {B}{right}{R}"]
    lv = f" ({old_version})" if old_version and old_version != otag else ""
    rv = f" ({new_version})" if new_version and new_version != ntag else ""
    return [f"   {nrepo}", f"   {otag or '(no tag)'}{lv} → {B}{ntag or '(no tag)'}{rv}{R}"]


class _Spinner:
    FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, render):
        self.render = render  # frame -> line text
        self.i = 0
        self.stop_evt = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)

    def start(self):
        self.draw()
        self.thread.start()

    def _loop(self):
        while not self.stop_evt.wait(0.1):
            with _LOCK:
                self.i += 1
                self.draw()

    def draw(self):
        try:
            sys.stdout.write("\r\033[K" + self.render(self.FRAMES[self.i % len(self.FRAMES)]))
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def clear(self):
        try:
            sys.stdout.write("\r\033[K")
            sys.stdout.flush()
        except (OSError, ValueError):
            pass

    def stop(self):
        self.stop_evt.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)
        with _LOCK:
            self.clear()


class _Step:
    def __init__(self, label: str, num: int, total: int):
        self.label, self.num, self.total = label, num, total
        self.notes: list[str] = []
        self.spinner: _Spinner | None = None
        self.start = time.monotonic()
        self.result: str | None = None  # override the ✓ text, e.g. "skipped"

    def line(self, mark: str, dur: str) -> str:
        label = self.label if len(self.label) <= LABEL_W else self.label[: LABEL_W - 1] + "…"
        return f" {D}[{self.num}/{self.total}]{R} {label:<{LABEL_W}} {mark} {dur:>7}"


class Timeline:
    """Numbered steps with timings, downtime tracking and a closing summary."""

    def __init__(self, total_steps: int):
        self.total = total_steps
        self.n = 0
        self.t0 = time.monotonic()
        self.down_start: float | None = None
        self.down_end: float | None = None

    # header ---------------------------------------------------------------------------
    def header(self, name: str, what: str, verb: str, old: str | None = None,
               new: str | None = None, reason: str | None = None,
               old_version: str | None = None, new_version: str | None = None,
               old_digest: str | None = None, new_digest: str | None = None) -> None:
        head = f"━━ {name} · {what} · {verb} "
        _write(f"\n{B}{M}{head}{'━' * max(3, _width() - len(head))}{R}", sys.stdout)
        if new:
            for line in image_lines(old, new, old_version, new_version, old_digest, new_digest):
                _write(line, sys.stdout)
            if reason:
                _write(f"   {D}({reason}){R}", sys.stdout)
        _write("", sys.stdout)

    # steps ----------------------------------------------------------------------------
    @contextmanager
    def step(self, label: str):
        global _step
        self.n += 1
        st = _Step(label, self.n, self.total)
        with _LOCK:
            _step = st
        if sys.stdout.isatty() and not verbose():
            st.spinner = _Spinner(lambda f: st.line(f"{M}{f}{R}", fmt_duration(time.monotonic() - st.start)))
            st.spinner.start()
        elif verbose():
            _write(f" {D}[{st.num}/{st.total}]{R} {label} …", sys.stdout)
        ok_ = False
        try:
            yield st
            ok_ = True
        finally:
            if st.spinner:
                st.spinner.stop()
            with _LOCK:
                _step = None
            dur = fmt_duration(time.monotonic() - st.start)
            if ok_:
                mark = f"{D}–{R}" if st.result else f"{G}✓{R}"
                _write(st.line(mark, st.result or dur), sys.stdout)
            else:
                _write(st.line(f"{RED}✗{R}", dur), sys.stdout)
            for note in st.notes:
                _write(note, sys.stdout)

    def note(self, msg: str) -> None:
        """Indented line under the current step's result (or right away outside a step)."""
        _out(f"       {D}{msg}{R}")

    def downtime_start(self) -> None:
        self.down_start = time.monotonic()
        self.note("── downtime starts ──")

    def downtime_end(self) -> None:
        if self.down_start is not None and self.down_end is None:
            self.down_end = time.monotonic()

    # summary --------------------------------------------------------------------------
    def _times(self) -> str:
        total = fmt_duration(time.monotonic() - self.t0)
        if self.down_start is None:
            return total
        end = self.down_end if self.down_end is not None else time.monotonic()
        tail = "" if self.down_end is not None else " (CT not running)"
        return f"{total} · downtime {fmt_duration(end - self.down_start)}{tail}"

    def done(self, verb: str, extra: list[tuple[str, str]] | None = None) -> None:
        _write(f"\n {G}✓{R} {B}{verb} in {self._times()}{R}", sys.stdout)
        for k, v in extra or []:
            _write(f"   {D}{k:<8}{R} {v}" if k else f"   {v}", sys.stdout)

    def failed(self, verb: str, extra: list[tuple[str, str]] | None = None) -> None:
        _write(f"\n {RED}✗ {verb} failed after {self._times()}{R}", sys.stdout)
        for k, v in extra or []:
            _write(f"   {D}{k:<8}{R} {v}" if k else f"   {v}", sys.stdout)


def summarize_paths(paths: list[str], limit: int = 2) -> str:
    """'6 files injected (/a, /b, +4 more)'."""
    n = len(paths)
    if not n:
        return "no files injected"
    shown = paths if verbose() or n <= limit + 1 else paths[:limit]
    more = f", +{n - len(shown)} more" if len(shown) < n else ""
    return f"{n} file{'s' if n != 1 else ''} injected ({', '.join(shown)}{more})"
